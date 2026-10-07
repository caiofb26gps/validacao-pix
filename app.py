import hashlib
import io
import os
import secrets
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (
    Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for,
)
from openpyxl import Workbook
from sqlalchemy import case, func, select, update
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import email_graph
from db import agora, criar_tabelas, engine, lote, redefinicao_senha, usuario, validacao
from importacao import ArquivoInvalido, ler_arquivo
from processador import CotaExcedida, consultar_status, criar_lote, preparar_na_subida, uso_no_mes

LIMITE_PADRAO = int(os.environ.get("LIMITE_MENSAL_PADRAO", "500"))
MAX_LINHAS = int(os.environ.get("MAX_LINHAS_ARQUIVO", "5000"))

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SEGURO", "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=8 * 3600,
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
)
if not os.environ.get("SECRET_KEY"):
    print("AVISO: SECRET_KEY não definida — sessões caem a cada reinício do app.")
# Atrás do proxy do Render: sem isso request.remote_addr (usado no limite de
# tentativas de login) seria sempre o IP do proxy.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)


def criar_admin_inicial():
    """Em produção não há terminal para rodar criar_admin.py: se a tabela de
    usuários estiver vazia e ADMIN_LOGIN/ADMIN_SENHA existirem, cria o admin.
    Com qualquer usuário já cadastrado, não faz nada."""
    login_, senha = os.environ.get("ADMIN_LOGIN", "").strip().lower(), os.environ.get("ADMIN_SENHA", "")
    if not login_ or not senha:
        return
    with engine.begin() as conn:
        if conn.execute(select(func.count()).select_from(usuario)).scalar_one():
            return
        conn.execute(usuario.insert().values(
            nome=os.environ.get("ADMIN_NOME", login_), login=login_, senha_hash=generate_password_hash(senha),
            admin=True, ativo=True, limite_mensal=LIMITE_PADRAO, criado_em=agora(),
        ))
    print(f"Admin inicial {login_} criado.")


criar_tabelas()
criar_admin_inicial()
preparar_na_subida()


# ---------------------------------------------------------------- segurança

_tentativas: dict[str, list[float]] = defaultdict(list)
MAX_TENTATIVAS, JANELA_TENTATIVAS = 5, 15 * 60


def _bloqueado(chave: str) -> bool:
    limite = time.time() - JANELA_TENTATIVAS
    _tentativas[chave] = [t for t in _tentativas[chave] if t > limite]
    return len(_tentativas[chave]) >= MAX_TENTATIVAS


@app.before_request
def carregar_usuario_e_checar_csrf():
    g.usuario = None
    if "usuario_id" in session:
        with engine.connect() as conn:
            u = conn.execute(select(usuario).where(usuario.c.id == session["usuario_id"])).one_or_none()
        if u and u.ativo:
            g.usuario = u
        else:
            session.clear()

    if request.method == "POST":
        token = request.form.get("csrf")
        if not token or token != session.get("csrf"):
            abort(400, "Formulário expirado — recarregue a página e tente de novo.")


@app.context_processor
def injetar_csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return {"csrf": session["csrf"]}


ROTULOS = {
    "pendente": "na fila", "enviando": "enviando", "aguardando": "aguardando retorno",
    "ok": "válida", "invalida": "inválida", "erro": "erro",
}
app.jinja_env.globals["ROTULOS"] = ROTULOS


@app.template_global()
def situacao_lote(c):
    """(classe css, texto) do lote a partir da contagem de linhas por status
    — `concluido` sozinho ficava verde mesmo com todas as linhas em erro."""
    if c["pendente"] or c["enviando"]:
        return "processando", "enviando"
    if c["aguardando"]:
        return "processando", "aguardando retorno"
    problemas = c["invalida"] + c["erro"]
    if not problemas:
        return "ok", "concluído"
    if not c["ok"]:
        return "erro", "concluído sem válidas"
    return "parcial", "concluído com pendências"


def exige_login(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not g.usuario:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def exige_admin(f):
    @wraps(f)
    @exige_login
    def wrapper(*args, **kwargs):
        if not g.usuario.admin:
            abort(403)
        return f(*args, **kwargs)
    return wrapper


@app.get("/healthz")
def healthz():
    # Não toca no banco de propósito: o health check do Render e o ping
    # externo que mantém o serviço acordado batem aqui o tempo todo — com uma
    # query, o Neon (free) nunca suspenderia e queimaria as horas de compute.
    return {"ok": True}


# ---------------------------------------------------------------- login

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        login_ = request.form.get("login", "").strip().lower()
        chave = f"{request.remote_addr}|{login_}"
        if _bloqueado(chave):
            flash("Muitas tentativas. Aguarde 15 minutos.", "erro")
            return render_template("login.html"), 429

        with engine.connect() as conn:
            u = conn.execute(select(usuario).where(usuario.c.login == login_)).one_or_none()
        if u and u.ativo and check_password_hash(u.senha_hash, request.form.get("senha", "")):
            _tentativas.pop(chave, None)
            session.clear()
            session.permanent = True
            session["usuario_id"] = u.id
            return redirect(url_for("inicio"))

        _tentativas[chave].append(time.time())
        flash("Login ou senha inválidos.", "erro")
    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


VALIDADE_LINK = timedelta(hours=1)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@app.route("/esqueci-senha", methods=["GET", "POST"])
def esqueci_senha():
    if request.method == "POST":
        login_ = request.form.get("login", "").strip().lower()
        chave = f"esqueci|{request.remote_addr}"
        if _bloqueado(chave):
            flash("Muitas solicitações. Aguarde 15 minutos.", "erro")
            return render_template("esqueci.html"), 429
        _tentativas[chave].append(time.time())

        if not email_graph.configurado():
            flash("Recuperação por e-mail não está configurada. Fale com o administrador.", "erro")
            return render_template("esqueci.html")

        with engine.connect() as conn:
            u = conn.execute(select(usuario).where(usuario.c.login == login_)).one_or_none()
        if u and u.ativo and "@" in u.login:
            token = secrets.token_urlsafe(32)
            with engine.begin() as conn:
                conn.execute(redefinicao_senha.insert().values(
                    usuario_id=u.id, token_hash=_hash_token(token),
                    expira_em=agora() + VALIDADE_LINK, criado_em=agora(),
                ))
            link = url_for("redefinir_senha", token=token, _external=True)
            try:
                email_graph.enviar_email(u.login, "Validação PIX — redefinição de senha", render_template(
                    "email_redefinicao.html", nome=u.nome, link=link))
            except Exception as e:
                print(f"Falha ao enviar e-mail de redefinição para {u.login}: {e}")
        # Mesma resposta exista ou não o login — não revela quem tem conta.
        flash("Se esse e-mail estiver cadastrado, você vai receber um link para criar uma nova senha "
              "(válido por 1 hora). Confira também o lixo eletrônico.", "ok")
        return redirect(url_for("login"))
    return render_template("esqueci.html")


@app.route("/redefinir-senha/<token>", methods=["GET", "POST"])
def redefinir_senha(token):
    with engine.connect() as conn:
        pedido = conn.execute(select(redefinicao_senha).where(
            redefinicao_senha.c.token_hash == _hash_token(token),
            redefinicao_senha.c.usado_em.is_(None),
            redefinicao_senha.c.expira_em > agora(),
        )).one_or_none()
    if not pedido:
        flash("Link inválido ou expirado. Peça um novo.", "erro")
        return redirect(url_for("esqueci_senha"))

    if request.method == "POST":
        nova = request.form.get("nova", "")
        if len(nova) < 8:
            flash("A nova senha precisa ter pelo menos 8 caracteres.", "erro")
        elif nova != request.form.get("confirmacao"):
            flash("A confirmação não confere.", "erro")
        else:
            with engine.begin() as conn:
                conn.execute(update(usuario).where(usuario.c.id == pedido.usuario_id)
                             .values(senha_hash=generate_password_hash(nova)))
                # Invalida este e qualquer outro link pendente do mesmo usuário
                conn.execute(update(redefinicao_senha)
                             .where(redefinicao_senha.c.usuario_id == pedido.usuario_id,
                                    redefinicao_senha.c.usado_em.is_(None))
                             .values(usado_em=agora()))
            flash("Senha alterada. Entre com a nova senha.", "ok")
            return redirect(url_for("login"))
    return render_template("redefinir.html")


@app.route("/senha", methods=["GET", "POST"])
@exige_login
def trocar_senha():
    if request.method == "POST":
        nova = request.form.get("nova", "")
        if not check_password_hash(g.usuario.senha_hash, request.form.get("atual", "")):
            flash("Senha atual incorreta.", "erro")
        elif len(nova) < 8:
            flash("A nova senha precisa ter pelo menos 8 caracteres.", "erro")
        elif nova != request.form.get("confirmacao"):
            flash("A confirmação não confere.", "erro")
        else:
            with engine.begin() as conn:
                conn.execute(update(usuario).where(usuario.c.id == g.usuario.id)
                             .values(senha_hash=generate_password_hash(nova)))
            flash("Senha alterada.", "ok")
            return redirect(url_for("inicio"))
    return render_template("senha.html")


# ---------------------------------------------------------------- lotes

def _contar_por_status():
    return [func.coalesce(func.sum(case((validacao.c.status == s, 1), else_=0)), 0).label(s) for s in ROTULOS]


def _resumo_lotes(conn, usuario_id=None, de=None, ate=None, limite=100):
    """Lotes com a contagem de linhas por status em `l.contagem`.
    `de`/`ate` são datetimes, intervalo [de, ate)."""
    q = (
        select(lote, usuario.c.nome.label("usuario_nome"), *_contar_por_status())
        .join(usuario, usuario.c.id == lote.c.usuario_id)
        .outerjoin(validacao, validacao.c.lote_id == lote.c.id)
        .group_by(*lote.c, usuario.c.nome)
        .order_by(lote.c.id.desc())
        .limit(limite)
    )
    if usuario_id is not None:
        q = q.where(lote.c.usuario_id == usuario_id)
    if de is not None:
        q = q.where(lote.c.criado_em >= de)
    if ate is not None:
        q = q.where(lote.c.criado_em < ate)
    return [{**r._mapping, "contagem": {s: r._mapping[s] for s in ROTULOS}} for r in conn.execute(q)]


def _data_param(nome: str, padrao: date) -> date:
    try:
        return date.fromisoformat(request.args.get(nome, ""))
    except ValueError:
        return padrao


@app.get("/relatorio")
@exige_login
def relatorio():
    hoje = agora().date()
    de = _data_param("de", hoje.replace(day=1))
    ate = _data_param("ate", hoje)
    if ate < de:
        de, ate = ate, de
    inicio_dt = datetime.combine(de, datetime.min.time())
    fim_dt = datetime.combine(ate + timedelta(days=1), datetime.min.time())  # inclui o dia final inteiro

    # Usuário comum só enxerga os próprios números; admin escolhe (ou vê todos).
    usuario_id = request.args.get("usuario", type=int) if g.usuario.admin else g.usuario.id

    filtro = [validacao.c.criado_em >= inicio_dt, validacao.c.criado_em < fim_dt]
    if usuario_id:
        filtro.append(validacao.c.usuario_id == usuario_id)

    with engine.connect() as conn:
        total = conn.execute(select(func.count(), *_contar_por_status()).select_from(validacao).where(*filtro)).one()
        por_usuario = conn.execute(
            select(usuario.c.id, usuario.c.nome, func.count().label("total"), *_contar_por_status())
            .select_from(validacao).join(usuario, usuario.c.id == validacao.c.usuario_id)
            .where(*filtro).group_by(usuario.c.id, usuario.c.nome).order_by(func.count().desc())
        ).all()
        por_tipo = conn.execute(
            select(func.upper(validacao.c.tipo_chave).label("tipo"), func.count().label("total"), *_contar_por_status())
            .where(*filtro).group_by(func.upper(validacao.c.tipo_chave)).order_by(func.count().desc())
        ).all()
        lotes = _resumo_lotes(conn, usuario_id or None, inicio_dt, fim_dt, limite=500)
        usuarios = conn.execute(select(usuario.c.id, usuario.c.nome).order_by(usuario.c.nome)).all() \
            if g.usuario.admin else []

    fim_mes_passado = hoje.replace(day=1) - timedelta(days=1)
    atalhos = [
        ("este mês", hoje.replace(day=1), hoje),
        ("mês passado", fim_mes_passado.replace(day=1), fim_mes_passado),
        ("últimos 30 dias", hoje - timedelta(days=29), hoje),
        ("este ano", hoje.replace(month=1, day=1), hoje),
    ]
    return render_template("relatorio.html", de=de, ate=ate, usuario_id=usuario_id, usuarios=usuarios, atalhos=atalhos,
                           total=total, por_usuario=por_usuario, por_tipo=por_tipo, lotes=lotes)


@app.get("/")
@exige_login
def inicio():
    with engine.connect() as conn:
        usado = uso_no_mes(conn, g.usuario.id)
        lotes = _resumo_lotes(conn, g.usuario.id)
    return render_template("inicio.html", usado=usado, limite=g.usuario.limite_mensal,
                           lotes=lotes, max_linhas=MAX_LINHAS)


@app.post("/importar")
@exige_login
def importar():
    arquivo = request.files.get("arquivo")
    if not arquivo or not arquivo.filename:
        flash("Selecione um arquivo.", "erro")
        return redirect(url_for("inicio"))

    try:
        linhas, problemas = ler_arquivo(arquivo.filename, arquivo.read())
    except ArquivoInvalido as e:
        flash(str(e), "erro")
        return redirect(url_for("inicio"))

    if not linhas:
        flash("Nenhuma linha válida no arquivo. Nada foi enviado.", "erro")
        for p in problemas[:20]:
            flash(p, "aviso")
        return redirect(url_for("inicio"))

    if len(linhas) > MAX_LINHAS:
        flash(f"O arquivo tem {len(linhas)} linhas válidas; o máximo por arquivo é {MAX_LINHAS}. "
              "Nada foi enviado — divida o arquivo.", "erro")
        return redirect(url_for("inicio"))

    try:
        lote_id = criar_lote(g.usuario.id, arquivo.filename[:255], linhas, len(problemas))
    except CotaExcedida as e:
        flash(str(e), "erro")
        return redirect(url_for("inicio"))

    flash(f"{len(linhas)} chaves enviadas para validação.", "ok")
    if problemas:
        flash(f"{len(problemas)} linha(s) ignorada(s) por estarem incompletas/inválidas (não foram cobradas):", "aviso")
        for p in problemas[:20]:
            flash(p, "aviso")
        if len(problemas) > 20:
            flash(f"... e mais {len(problemas) - 20}.", "aviso")
    return redirect(url_for("ver_lote", lote_id=lote_id))


def _carregar_lote(lote_id: int):
    with engine.connect() as conn:
        l = conn.execute(
            select(lote, usuario.c.nome.label("usuario_nome"))
            .join(usuario, usuario.c.id == lote.c.usuario_id)
            .where(lote.c.id == lote_id)
        ).one_or_none()
    if not l or (not g.usuario.admin and l.usuario_id != g.usuario.id):
        abort(404)
    return l


@app.get("/lote/<int:lote_id>")
@exige_login
def ver_lote(lote_id):
    l = _carregar_lote(lote_id)
    with engine.connect() as conn:
        linhas = conn.execute(
            select(validacao).where(validacao.c.lote_id == lote_id).order_by(validacao.c.linha)
        ).all()
    contagem = {s: sum(1 for v in linhas if v.status == s) for s in ROTULOS}
    return render_template("lote.html", lote=l, linhas=linhas, contagem=contagem)


@app.post("/lote/<int:lote_id>/consultar")
@exige_login
def consultar_lote(lote_id):
    _carregar_lote(lote_id)
    threading.Thread(target=consultar_status, args=(lote_id,), daemon=True).start()
    flash("Consultando o retorno no GPS Pay — a página atualiza sozinha.", "ok")
    return redirect(url_for("ver_lote", lote_id=lote_id))


@app.get("/lote/<int:lote_id>/download")
@exige_login
def baixar_lote(lote_id):
    l = _carregar_lote(lote_id)
    with engine.connect() as conn:
        linhas = conn.execute(
            select(validacao).where(validacao.c.lote_id == lote_id).order_by(validacao.c.linha)
        ).all()

    wb = Workbook()
    ws = wb.active
    ws.title = "Validacao"
    ws.append(["linha_arquivo", "cpf", "tipochave", "chave", "situacao", "status_gps", "mensagem",
               "http_envio", "id_integracao", "resposta_envio", "enviado_em", "consultado_em"])
    for v in linhas:
        ws.append([v.linha, v.cpf, v.tipo_chave, v.chave, ROTULOS.get(v.status, v.status), v.status_gps,
                   v.mensagem, v.status_code, v.id_integracao, v.resposta, v.processado_em, v.consultado_em])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"validacao_pix_lote_{l.id}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/modelo.xlsx")
@exige_login
def modelo():
    wb = Workbook()
    ws = wb.active
    ws.append(["cpf", "tipochave", "chave"])
    for col in "ABC":
        ws.column_dimensions[col].number_format = "@"
        ws.column_dimensions[col].width = 24
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="modelo_validacao_pix.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------- admin

@app.get("/admin/usuarios")
@exige_admin
def admin_usuarios():
    with engine.connect() as conn:
        usuarios = conn.execute(select(usuario).order_by(usuario.c.nome)).all()
        uso = {u.id: uso_no_mes(conn, u.id) for u in usuarios}
    return render_template("usuarios.html", usuarios=usuarios, uso=uso, limite_padrao=LIMITE_PADRAO)


@app.post("/admin/usuarios")
@exige_admin
def admin_criar_usuario():
    nome = request.form.get("nome", "").strip()
    login_ = request.form.get("login", "").strip().lower()
    senha = request.form.get("senha", "")
    if not nome or not login_ or len(senha) < 8:
        flash("Preencha nome, login e uma senha com pelo menos 8 caracteres.", "erro")
        return redirect(url_for("admin_usuarios"))
    with engine.begin() as conn:
        if conn.execute(select(usuario.c.id).where(usuario.c.login == login_)).first():
            flash(f"Já existe um usuário com o login {login_}.", "erro")
            return redirect(url_for("admin_usuarios"))
        conn.execute(usuario.insert().values(
            nome=nome, login=login_, senha_hash=generate_password_hash(senha),
            admin=request.form.get("admin") == "on", ativo=True,
            limite_mensal=max(int(request.form.get("limite_mensal") or LIMITE_PADRAO), 0),
            criado_em=agora(),
        ))
    flash(f"Usuário {login_} criado.", "ok")
    return redirect(url_for("admin_usuarios"))


@app.post("/admin/usuarios/<int:usuario_id>")
@exige_admin
def admin_editar_usuario(usuario_id):
    valores = {
        "limite_mensal": max(int(request.form.get("limite_mensal") or 0), 0),
        "ativo": request.form.get("ativo") == "on",
        "admin": request.form.get("admin") == "on",
    }
    if usuario_id == g.usuario.id and (not valores["ativo"] or not valores["admin"]):
        flash("Você não pode desativar nem tirar o admin de si mesmo.", "erro")
        return redirect(url_for("admin_usuarios"))
    nova_senha = request.form.get("nova_senha", "")
    if nova_senha:
        if len(nova_senha) < 8:
            flash("A nova senha precisa ter pelo menos 8 caracteres.", "erro")
            return redirect(url_for("admin_usuarios"))
        valores["senha_hash"] = generate_password_hash(nova_senha)
    with engine.begin() as conn:
        conn.execute(update(usuario).where(usuario.c.id == usuario_id).values(**valores))
    flash("Usuário atualizado.", "ok")
    return redirect(url_for("admin_usuarios"))


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def erro_http(e):
    return render_template("erro.html", erro=e), e.code


if __name__ == "__main__":
    from waitress import serve
    porta = int(os.environ.get("PORT", "8080"))
    print(f"Validação PIX rodando em http://localhost:{porta}")
    serve(app, host="0.0.0.0", port=porta, threads=8)
