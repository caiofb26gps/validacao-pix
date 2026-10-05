import io
import os
import secrets
import time
from collections import defaultdict
from functools import wraps

from flask import (
    Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for,
)
from openpyxl import Workbook
from sqlalchemy import case, func, select, update
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from db import agora, criar_tabelas, engine, lote, usuario, validacao
from importacao import ArquivoInvalido, ler_arquivo
from processador import CotaExcedida, criar_lote, recuperar_interrompidos, uso_no_mes

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
recuperar_interrompidos()


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


@app.template_global()
def situacao_lote(status, ok, erro):
    """(classe css, texto) do status do lote — `concluido` sozinho ficava
    verde mesmo com todas as linhas em erro."""
    if status != "concluido":
        return "processando", "processando"
    if not erro:
        return "ok", "concluído"
    if not ok:
        return "erro", "concluído com erro"
    return "parcial", "concluído com erros"


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

def _resumo_lotes(conn, usuario_id=None):
    q = (
        select(
            lote, usuario.c.nome.label("usuario_nome"),
            func.sum(case((validacao.c.status == "ok", 1), else_=0)).label("ok"),
            func.sum(case((validacao.c.status == "erro", 1), else_=0)).label("erro"),
        )
        .join(usuario, usuario.c.id == lote.c.usuario_id)
        .outerjoin(validacao, validacao.c.lote_id == lote.c.id)
        .group_by(*lote.c, usuario.c.nome)
        .order_by(lote.c.id.desc())
        .limit(100)
    )
    if usuario_id is not None:
        q = q.where(lote.c.usuario_id == usuario_id)
    return conn.execute(q).all()


@app.get("/")
@exige_login
def inicio():
    with engine.connect() as conn:
        usado = uso_no_mes(conn, g.usuario.id)
        lotes = _resumo_lotes(conn, None if g.usuario.admin else g.usuario.id)
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
    contagem = {s: sum(1 for v in linhas if v.status == s) for s in ("pendente", "enviando", "ok", "erro")}
    return render_template("lote.html", lote=l, linhas=linhas, contagem=contagem)


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
    ws.append(["linha_arquivo", "cpf", "tipochave", "chave", "status", "status_code", "resposta", "processado_em"])
    for v in linhas:
        ws.append([v.linha, v.cpf, v.tipo_chave, v.chave, v.status, v.status_code, v.resposta, v.processado_em])
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
