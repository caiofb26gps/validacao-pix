"""Cria (ou redefine a senha de) um usuário admin. Uso:
    py criar_admin.py <login> "<nome>"
A senha é pedida no terminal (não fica no histórico do shell)."""

import os
import sys
from getpass import getpass

from sqlalchemy import select, update
from werkzeug.security import generate_password_hash

from db import agora, criar_tabelas, engine, usuario

if len(sys.argv) < 3:
    sys.exit(__doc__)

login, nome = sys.argv[1].strip().lower(), sys.argv[2].strip()
senha = getpass("Senha (mín. 8 caracteres): ")
if len(senha) < 8 or senha != getpass("Confirme a senha: "):
    sys.exit("Senha curta ou confirmação diferente.")

criar_tabelas()
with engine.begin() as conn:
    existente = conn.execute(select(usuario.c.id).where(usuario.c.login == login)).first()
    if existente:
        conn.execute(update(usuario).where(usuario.c.id == existente.id).values(
            senha_hash=generate_password_hash(senha), admin=True, ativo=True))
        print(f"Usuário {login} já existia — senha redefinida e marcado como admin.")
    else:
        conn.execute(usuario.insert().values(
            nome=nome, login=login, senha_hash=generate_password_hash(senha), admin=True, ativo=True,
            limite_mensal=int(os.environ.get("LIMITE_MENSAL_PADRAO", "500")), criado_em=agora()))
        print(f"Admin {login} criado.")
