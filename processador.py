"""Reserva de cota e envio das linhas para a API de validação do GPS Pay.

Regra de custo: cada linha gravada em pix_validacao é UMA chamada paga à API.
A cota é reservada na hora da importação (as linhas entram como `pendente`
dentro da mesma transação que confere o saldo), então duas importações
simultâneas do mesmo usuário nunca furam o limite.
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import requests
from sqlalchemy import func, select, text, update

from db import POSTGRES, agora, engine, inicio_do_mes, lote, usuario, validacao

API_URL = os.environ.get("GPS_PAY_URL", "")
API_TOKEN = os.environ.get("GPS_PAY_TOKEN", "")
TEAMS_WEBHOOK = os.environ.get("TEAMS_WEBHOOK", "")
WORKERS = int(os.environ.get("WORKERS_API", "5"))

_reserva_lock = threading.Lock()
_lotes_em_execucao: set[int] = set()
_execucao_lock = threading.Lock()


class CotaExcedida(Exception):
    pass


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


def _chamar_api(cpf: str, tipo_chave: str, chave: str) -> tuple[int, str]:
    try:
        r = requests.post(
            API_URL,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_TOKEN}"},
            json={"cpf": cpf, "tipoChave": tipo_chave, "chave": chave},
            timeout=30,
        )
        return r.status_code, r.text
    except Exception as e:  # rede, timeout, DNS...
        return 0, str(e)


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
                status="ok" if 200 <= status_code < 300 else "erro",
                status_code=status_code,
                resposta=resposta[:4000],
                processado_em=agora(),
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

        with engine.begin() as conn:
            conn.execute(update(lote).where(lote.c.id == lote_id).values(status="concluido", concluido_em=agora()))
        _avisar_teams(lote_id)
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


def recuperar_interrompidos():
    """Na subida do app: linhas que estavam `enviando` quando o processo caiu
    podem já ter sido cobradas — viram erro (não reenvia sozinho, por custo).
    As `pendente` nunca foram enviadas, então o lote continua de onde parou."""
    with engine.begin() as conn:
        conn.execute(
            update(validacao).where(validacao.c.status == "enviando").values(
                status="erro", status_code=None, processado_em=agora(),
                resposta="Processamento interrompido durante a chamada — a cobrança pode ter ocorrido. Confira antes de reenviar.",
            )
        )
        abertos = conn.execute(select(lote.c.id).where(lote.c.status == "processando")).scalars().all()
    for lote_id in abertos:
        iniciar_processamento(lote_id)


def _avisar_teams(lote_id: int):
    if not TEAMS_WEBHOOK:
        return
    try:
        with engine.connect() as conn:
            info = conn.execute(text("""
                SELECT l.arquivo, u.nome,
                       SUM(CASE WHEN v.status = 'ok' THEN 1 ELSE 0 END) AS ok,
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
            f"Total: {info.total} | Sucesso: {info.ok} | Erro: {info.erro}"
        )}, timeout=30)
    except Exception as e:
        print(f"Erro Teams: {e}")
