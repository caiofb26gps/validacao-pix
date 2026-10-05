"""Leitura do arquivo enviado (xlsx ou csv) e checagem das linhas ANTES de
gastar qualquer chamada à API: linha sem CPF/tipo/chave ou com CPF malformado
é descartada aqui e não conta na cota."""

import csv
import io
import re
import unicodedata

from openpyxl import load_workbook

COLUNAS = {"cpf": "cpf", "tipochave": "tipo_chave", "chave": "chave"}


class ArquivoInvalido(Exception):
    pass


def _normalizar_cabecalho(valor) -> str:
    texto = unicodedata.normalize("NFKD", str(valor or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", texto.lower())


def _texto(valor) -> str:
    if valor is None:
        return ""
    # Excel guarda CPF/telefone digitado como número: 12345678901 vira
    # 12345678901.0 (float) — sem isso a chave iria pra API como "1.2345678901E10".
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    return str(valor).strip()


def _ler_linhas(nome: str, conteudo: bytes) -> list[list]:
    if nome.lower().endswith(".xlsx"):
        try:
            wb = load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
        except Exception:
            raise ArquivoInvalido("Não consegui abrir o .xlsx — confira se o arquivo não está corrompido.")
        return [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]

    if nome.lower().endswith(".csv"):
        try:
            texto = conteudo.decode("utf-8-sig")
        except UnicodeDecodeError:
            texto = conteudo.decode("latin-1")
        delimitador = ";" if texto.split("\n", 1)[0].count(";") > texto.split("\n", 1)[0].count(",") else ","
        return [r for r in csv.reader(io.StringIO(texto), delimiter=delimitador)]

    raise ArquivoInvalido("Formato não suportado — envie .xlsx ou .csv.")


def cpf_valido(cpf: str) -> bool:
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    for tamanho in (9, 10):
        soma = sum(int(d) * peso for d, peso in zip(cpf[:tamanho], range(tamanho + 1, 1, -1)))
        if (soma * 10 % 11) % 10 != int(cpf[tamanho]):
            return False
    return True


def ler_arquivo(nome: str, conteudo: bytes) -> tuple[list[dict], list[str]]:
    """Devolve (linhas_validas, problemas). Cada linha válida tem
    linha/cpf/tipo_chave/chave; `problemas` descreve cada linha descartada."""
    linhas = _ler_linhas(nome, conteudo)
    if not linhas:
        raise ArquivoInvalido("Arquivo vazio.")

    cabecalho = [_normalizar_cabecalho(c) for c in linhas[0]]
    indices = {}
    for col_arquivo, campo in COLUNAS.items():
        if col_arquivo not in cabecalho:
            raise ArquivoInvalido(
                "Cabeçalho precisa ter as colunas cpf, tipochave e chave "
                f"(encontrei: {', '.join(str(c) for c in linhas[0] if c is not None)})."
            )
        indices[campo] = cabecalho.index(col_arquivo)

    validas, problemas = [], []
    for n, bruta in enumerate(linhas[1:], start=2):
        valores = {campo: _texto(bruta[i]) if i < len(bruta) else "" for campo, i in indices.items()}
        if not any(valores.values()):
            continue  # linha em branco no fim da planilha

        cpf = re.sub(r"\D", "", valores["cpf"])
        # Excel também come o zero à esquerda do CPF (012... vira 12...).
        if 0 < len(cpf) < 11:
            cpf = cpf.zfill(11)

        # Dígito verificador errado a API recusaria de qualquer jeito — mas
        # cobrando a chamada.
        if not cpf_valido(cpf):
            problemas.append(f"Linha {n}: CPF inválido ({valores['cpf'] or 'vazio'})")
        elif not valores["tipo_chave"]:
            problemas.append(f"Linha {n}: tipo de chave vazio")
        elif not valores["chave"]:
            problemas.append(f"Linha {n}: chave vazia")
        else:
            validas.append({"linha": n, "cpf": cpf, "tipo_chave": valores["tipo_chave"], "chave": valores["chave"]})

    return validas, problemas
