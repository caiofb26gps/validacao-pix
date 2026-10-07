import os
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, MetaData, String, Table, Text,
    create_engine, inspect, text,
)

load_dotenv()

FUSO = ZoneInfo("America/Sao_Paulo")

_url = os.environ.get("DATABASE_URL") or "sqlite:///pix.db"
# URL no formato padrão do Postgres (postgres://...) — o SQLAlchemy só aceita
# postgresql://, e sem o driver explícito.
for prefixo in ("postgres://", "postgresql://"):
    if _url.startswith(prefixo):
        _url = "postgresql+psycopg2://" + _url[len(prefixo):]

engine = create_engine(_url, pool_pre_ping=True, pool_size=10, max_overflow=5) if _url.startswith("postgresql") \
    else create_engine(_url, pool_pre_ping=True)
POSTGRES = engine.dialect.name == "postgresql"

metadata = MetaData()

usuario = Table(
    "pix_usuario", metadata,
    Column("id", Integer, primary_key=True),
    Column("nome", String(120), nullable=False),
    Column("login", String(80), nullable=False, unique=True),
    Column("senha_hash", String(255), nullable=False),
    Column("admin", Boolean, nullable=False, default=False),
    Column("ativo", Boolean, nullable=False, default=True),
    # Quantas chamadas à API (= quantas validações cobradas) o usuário pode
    # fazer por mês-calendário. 0 = bloqueado.
    Column("limite_mensal", Integer, nullable=False),
    Column("criado_em", DateTime, nullable=False),
)

lote = Table(
    "pix_lote", metadata,
    Column("id", Integer, primary_key=True),
    Column("usuario_id", Integer, ForeignKey("pix_usuario.id"), nullable=False),
    Column("arquivo", String(255), nullable=False),
    Column("total", Integer, nullable=False),       # linhas enviadas à API
    Column("ignoradas", Integer, nullable=False),   # linhas inválidas, não enviadas
    Column("status", String(20), nullable=False),   # processando | concluido
    Column("criado_em", DateTime, nullable=False),
    Column("concluido_em", DateTime),
)

validacao = Table(
    "pix_validacao", metadata,
    Column("id", Integer, primary_key=True),
    Column("lote_id", Integer, ForeignKey("pix_lote.id"), nullable=False, index=True),
    # Redundante com lote.usuario_id, mas é a coluna que a contagem da cota
    # mensal usa — evita join na checagem que roda a cada importação.
    Column("usuario_id", Integer, ForeignKey("pix_usuario.id"), nullable=False, index=True),
    Column("linha", Integer, nullable=False),
    Column("cpf", String(20), nullable=False),
    Column("tipo_chave", String(40), nullable=False),
    Column("chave", String(255), nullable=False),
    # pendente -> enviando -> aguardando (GPS Pay validando) -> ok | invalida
    #                      \-> erro (falha no envio, CPF fora da SRA, sem retorno)
    Column("status", String(20), nullable=False),
    Column("status_code", Integer),
    Column("resposta", Text),
    Column("criado_em", DateTime, nullable=False, index=True),
    Column("processado_em", DateTime),
    # id devolvido pelo iniciar-validacao, usado no GET status-validacao/{id}
    Column("id_integracao", String(100)),
    Column("status_gps", String(40)),   # status cru do status-validacao (ex: VALIDO)
    Column("mensagem", Text),           # explicação legível (errorMessage, SRA, prazo...)
    Column("consultado_em", DateTime),
)


redefinicao_senha = Table(
    "pix_redefinicao_senha", metadata,
    Column("id", Integer, primary_key=True),
    Column("usuario_id", Integer, ForeignKey("pix_usuario.id"), nullable=False),
    # sha256 do token — o token em si só existe no link do e-mail
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("expira_em", DateTime, nullable=False),
    Column("usado_em", DateTime),
    Column("criado_em", DateTime, nullable=False),
)


def agora() -> datetime:
    return datetime.now(FUSO).replace(tzinfo=None)


def inicio_do_mes() -> datetime:
    return agora().replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def criar_tabelas():
    metadata.create_all(engine)
    # create_all não altera tabela que já existe: colunas novas (todas
    # nullable) entram aqui. Sem framework de migração — o sistema é pequeno.
    for tabela in metadata.sorted_tables:
        existentes = {c["name"] for c in inspect(engine).get_columns(tabela.name)}
        with engine.begin() as conn:
            for col in tabela.columns:
                if col.name not in existentes:
                    tipo = col.type.compile(engine.dialect)
                    conn.execute(text(f"ALTER TABLE {tabela.name} ADD COLUMN {col.name} {tipo}"))
