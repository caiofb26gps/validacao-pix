"""Reserva de cota, envio das linhas para a API do GPS Pay e consulta do
retorno da validação.

Fluxo de uma linha:
  pendente -> enviando -> (POST iniciar-validacao)
     200 + id  -> aguardando -> (GET status-validacao/{id}, de tempos em tempos)
                     VALIDO        -> ok
                     outro status  -> invalida
                     sem retorno no prazo -> erro
     204       -> erro (CPF não encontrado na SRA)
     outros    -> erro

Regra de custo: cada linha gravada em pix_validacao é UMA chamada paga ao
iniciar-validacao. A cota é reservada na hora da importação (as linhas entram
como `pendente` dentro da mesma transação que confere o saldo), então duas
importações simultâneas do mesmo usuário nunca furam o limite. As consultas de
status não consomem cota.
"""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import requests
from sqlalchemy import func, or_, select, text, update

from db import POSTGRES, agora, engine, inicio_do_mes, lote, usuario, validacao

API_URL = os.environ.get("GPS_PAY_URL", "")
API_TOKEN = os.environ.get("GPS_PAY_TOKEN", "")
STATUS_URL = API_URL.rsplit("/iniciar-validacao", 1)[0] + "/status-validacao/"
TEAMS_WEBHOOK = os.environ.get("TEAMS_WEBHOOK", "")
WORKERS = int(os.environ.get("WORKERS_API", "5"))
INTERVALO_CONSULTA = int(os.environ.get("INTERVALO_CONSULTA_SEG", "120"))
PRAZO_RETORNO = timedelta(hours=int(os.environ.get("PRAZO_RETORNO_HORAS", "48")))

# Só VALIDO foi visto na prática; os demais nomes são palpite. Status que
# pareça "em andamento" segue aguardando; qualquer outro conta como inválida.
STATUS_VALIDO = {"VALIDO", "VALIDA"}
TRECHOS_EM_ANDAMENTO = ("PEND", "PROCESS", "AGUARD", "ANDAMENTO", "INICIAD", "VALIDANDO")

EM_ABERTO = ("pendente", "enviando", "aguardando")

_reserva_lock = threading.Lock()
_consulta_lock = threading.Lock()
_lotes_em_execucao: set[int] = set()
_execucao_lock = threading.Lock()


class CotaExcedida(Exception):
    pass


def _headers():
    return {"Content-Type": "application/json", "Authorization": f"Bearer {API_TOKEN}"}


def uso_no_mes(conn, usuario_id: int) -> int:
    return conn.execute(
        select(func.count()).select_from(validacao).where(
            validacao.c.usuario_id == usuario_id,
            validacao.c.criado_em >= inicio_do_mes(),
        )
    ).scalar_one()


def criar_lote(usuario_id: int, arquivo: str, linhas: list[dict], ignoradas: int) -> int:
    momento = agora()
    # O lock cobre o caso de um processo só (waitress); o FOR UPDATE cobre
    # o Postgres caso um dia rode mais de uma instância.
    with _reserva_lock, engine.begin() as conn:
        consulta = select(usuario.c.limite_mensal, usuario.c.ativo).where(usuario.c.id == usuario_id)
        if POSTGRES:
            consulta = consulta.with_for_update()
        u = conn.execute(consulta).one()
        if not u.ativo:
            raise CotaExcedida("Usuário inativo.")

        usado = uso_no_mes(conn, usuario_id)
        saldo = max(u.limite_mensal - usado, 0)
        if len(linhas) > saldo:
            raise CotaExcedida(
                f"O arquivo tem {len(linhas)} linhas válidas, mas seu saldo deste mês é de "
                f"{saldo} validações (limite {u.limite_mensal}, já usadas {usado}). "
                "Nada foi enviado. Divida o arquivo ou peça aumento de limite ao administrador."
            )

        lote_id = conn.execute(
            lote.insert().values(
                usuario_id=usuario_id, arquivo=arquivo, total=len(linhas), ignoradas=ignoradas,
                status="processando", criado_em=momento,
            ).returning(lote.c.id)
        ).scalar_one()

        conn.execute(validacao.insert(), [
            {**l, "lote_id": lote_id, "usuario_id": usuario_id, "status": "pendente", "criado_em": momento}
            for l in linhas
        ])

    iniciar_processamento(lote_id)
    return lote_id


# ---------------------------------------------------------------- envio

def _chamar_api(cpf: str, tipo_chave: str, chave: str) -> tuple[int, str]:
    try:
        r = requests.post(API_URL, headers=_headers(),
                          json={"cpf": cpf, "tipoChave": tipo_chave, "chave": chave}, timeout=30)
        return r.status_code, r.text
    except Exception as e:  # rede, timeout, DNS...
        return 0, str(e)


def _id_da_resposta(resposta: str):
    try:
        return (json.loads(resposta) or {}).get("id")
    except (ValueError, AttributeError):
        return None


def _resultado_envio(status_code: int, resposta: str) -> dict:
    if status_code == 204:
        return {"status": "erro", "mensagem": "CPF não encontrado na SRA"}
    if status_code == 200:
        id_ = _id_da_resposta(resposta)
        if id_:
            return {"status": "aguardando", "id_integracao": str(id_)[:100]}
        return {"status": "erro", "mensagem": "GPS Pay não devolveu o id da validação"}
    return {"status": "erro"}


def _processar_linha(linha_id: int):
    with engine.begin() as conn:
        # Marca `enviando` ANTES da chamada: se o processo cair no meio, a
        # linha não volta a ser enviada sozinha (ver recuperar_interrompidos).
        alteradas = conn.execute(
            update(validacao)
            .where(validacao.c.id == linha_id, validacao.c.status == "pendente")
            .values(status="enviando")
        ).rowcount
        if not alteradas:
            return
        l = conn.execute(select(validacao).where(validacao.c.id == linha_id)).one()

    status_code, resposta = _chamar_api(l.cpf, l.tipo_chave, l.chave)

    with engine.begin() as conn:
        conn.execute(
            update(validacao).where(validacao.c.id == linha_id).values(
                status_code=status_code,
                resposta=resposta[:4000],
                processado_em=agora(),
                **_resultado_envio(status_code, resposta),
            )
        )


def _manter_acordado(parar: threading.Event):
    """Render free desliga o serviço após 15 min sem requisição de fora — e
    leva junto a thread do lote. Enquanto houver lote rodando, o app chama a
    própria URL pública (passa pelo proxy do Render, conta como acesso)."""
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return
    while not parar.wait(240):
        try:
            requests.get(f"{url}/healthz", timeout=30)
        except Exception as e:
            print(f"keep-alive falhou: {e}")


def _executar_lote(lote_id: int):
    parar = threading.Event()
    threading.Thread(target=_manter_acordado, args=(parar,), daemon=True).start()
    try:
        with engine.connect() as conn:
            ids = conn.execute(
                select(validacao.c.id).where(validacao.c.lote_id == lote_id, validacao.c.status == "pendente")
                .order_by(validacao.c.linha)
            ).scalars().all()

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            for f in [executor.submit(_processar_linha, i) for i in ids]:
                try:
                    f.result()
                except Exception as e:
                    print(f"[lote {lote_id}] erro processando linha: {e}")

        # Primeira consulta logo em seguida: validações já conhecidas pelo
        # GPS Pay voltam na hora, sem esperar o ciclo do consultor.
        consultar_status(lote_id)
    finally:
        parar.set()
        with _execucao_lock:
            _lotes_em_execucao.discard(lote_id)


def iniciar_processamento(lote_id: int):
    with _execucao_lock:
        if lote_id in _lotes_em_execucao:
            return
        _lotes_em_execucao.add(lote_id)
    threading.Thread(target=_executar_lote, args=(lote_id,), daemon=True).start()


# ---------------------------------------------------------------- retorno

def _consultar_uma(linha) -> dict:
    momento = agora()
    valores = {"consultado_em": momento}
    try:
        r = requests.get(STATUS_URL + linha.id_integracao, headers=_headers(), timeout=30)
        dados = r.json() if r.status_code == 200 and r.content else None
    except Exception as e:
        print(f"consulta status {linha.id_integracao} falhou: {e}")
        dados = None

    if isinstance(dados, dict) and dados.get("status"):
        status_gps = str(dados["status"]).upper()
        valores["status_gps"] = status_gps[:40]
        if status_gps in STATUS_VALIDO:
            return {**valores, "status": "ok", "mensagem": None}
        if not any(t in status_gps for t in TRECHOS_EM_ANDAMENTO):
            return {**valores, "status": "invalida", "mensagem": dados.get("errorMessage") or status_gps}

    # 204 (ainda sem resultado), status em andamento ou falha de rede.
    if momento - linha.processado_em > PRAZO_RETORNO:
        horas = int(PRAZO_RETORNO.total_seconds() // 3600)
        return {**valores, "status": "erro", "mensagem": f"Sem retorno do GPS Pay em {horas}h"}
    return valores


def consultar_status(lote_id: int | None = None):
    """Consulta o status-validacao das linhas `aguardando` (todas, ou só de
    um lote). Não roda duas vezes em paralelo."""
    if not _consulta_lock.acquire(blocking=False):
        return
    try:
        q = select(validacao.c.id, validacao.c.lote_id, validacao.c.id_integracao, validacao.c.processado_em) \
            .where(validacao.c.status == "aguardando")
        if lote_id is not None:
            q = q.where(validacao.c.lote_id == lote_id)
        with engine.connect() as conn:
            linhas = conn.execute(q).all()

        def atualizar(linha):
            valores = _consultar_uma(linha)
            with engine.begin() as conn:
                conn.execute(update(validacao).where(validacao.c.id == linha.id).values(**valores))

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            list(executor.map(atualizar, linhas))

        lotes = {l.lote_id for l in linhas} | ({lote_id} if lote_id is not None else set())
        for lid in lotes:
            _finalizar_se_pronto(lid)
    finally:
        _consulta_lock.release()


def _finalizar_se_pronto(lote_id: int):
    with engine.begin() as conn:
        abertas = conn.execute(
            select(func.count()).select_from(validacao)
            .where(validacao.c.lote_id == lote_id, validacao.c.status.in_(EM_ABERTO))
        ).scalar_one()
        if abertas:
            return
        finalizou = conn.execute(
            update(lote).where(lote.c.id == lote_id, lote.c.status == "processando")
            .values(status="concluido", concluido_em=agora())
        ).rowcount
    if finalizou:
        _avisar_teams(lote_id)


def _loop_consulta():
    while True:
        threading.Event().wait(INTERVALO_CONSULTA)
        try:
            consultar_status()
        except Exception as e:
            print(f"consulta periódica falhou: {e}")


# ---------------------------------------------------------------- subida

def preparar_na_subida():
    """Roda uma vez quando o app sobe."""
    with engine.begin() as conn:
        # Linhas `enviando` quando o processo caiu podem já ter sido cobradas
        # — viram erro (não reenvia sozinho, por custo). As `pendente` nunca
        # foram enviadas, então o lote continua de onde parou.
        conn.execute(
            update(validacao).where(validacao.c.status == "enviando").values(
                status="erro", status_code=None, processado_em=agora(),
                mensagem="Processamento interrompido durante a chamada — a cobrança pode ter ocorrido. Confira antes de reenviar.",
            )
        )

        # Versão anterior marcava `ok` só por o POST ter dado 2xx, sem
        # consultar o retorno: reabre essas linhas para consulta.
        antigas = conn.execute(
            select(validacao.c.id, validacao.c.lote_id, validacao.c.status_code, validacao.c.resposta)
            .where(validacao.c.status == "ok", validacao.c.status_gps.is_(None),
                   or_(validacao.c.id_integracao.is_(None), validacao.c.status_code == 204))
        ).all()
        for a in antigas:
            conn.execute(update(validacao).where(validacao.c.id == a.id)
                         .values(**_resultado_envio(a.status_code, a.resposta or "")))
        if antigas:
            conn.execute(update(lote).where(lote.c.id.in_({a.lote_id for a in antigas}))
                         .values(status="processando", concluido_em=None))

        com_pendentes = conn.execute(
            select(validacao.c.lote_id).where(validacao.c.status == "pendente").distinct()
        ).scalars().all()
        abertos = conn.execute(select(lote.c.id).where(lote.c.status == "processando")).scalars().all()

    for lote_id in com_pendentes:
        iniciar_processamento(lote_id)
    for lote_id in set(abertos) - set(com_pendentes):
        _finalizar_se_pronto(lote_id)
    threading.Thread(target=_loop_consulta, daemon=True).start()


def _avisar_teams(lote_id: int):
    if not TEAMS_WEBHOOK:
        return
    try:
        with engine.connect() as conn:
            info = conn.execute(text("""
                SELECT l.arquivo, u.nome,
                       SUM(CASE WHEN v.status = 'ok' THEN 1 ELSE 0 END) AS ok,
                       SUM(CASE WHEN v.status = 'invalida' THEN 1 ELSE 0 END) AS invalida,
                       SUM(CASE WHEN v.status = 'erro' THEN 1 ELSE 0 END) AS erro,
                       COUNT(v.id) AS total
                FROM pix_lote l
                JOIN pix_usuario u ON u.id = l.usuario_id
                LEFT JOIN pix_validacao v ON v.lote_id = l.id
                WHERE l.id = :id
                GROUP BY l.arquivo, u.nome
            """), {"id": lote_id}).one()
        requests.post(TEAMS_WEBHOOK, json={"text": (
            f"✅ Validação PIX — lote #{lote_id} finalizado\n\n"
            f"Arquivo: {info.arquivo}\n\nUsuário: {info.nome}\n\n"
            f"Total: {info.total} | Válidas: {info.ok} | Inválidas: {info.invalida} | Erro: {info.erro}"
        )}, timeout=30)
    except Exception as e:
        print(f"Erro Teams: {e}")
