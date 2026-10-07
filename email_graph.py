"""Envio de e-mail pelo Microsoft Graph (mesmo app do Azure AD usado pelo
Disparos — o tenant tem Security Defaults, SMTP com senha não funciona)."""

import os
import time

import requests

_token = {"valor": None, "expira": 0.0}


def configurado() -> bool:
    return all(os.environ.get(v) for v in ("MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET", "MAIL_SENDER_UPN"))


def _obter_token() -> str:
    if _token["valor"] and time.time() < _token["expira"] - 60:
        return _token["valor"]
    r = requests.post(
        f"https://login.microsoftonline.com/{os.environ['MS_TENANT_ID']}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": os.environ["MS_CLIENT_ID"],
            "client_secret": os.environ["MS_CLIENT_SECRET"],
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=30,
    )
    r.raise_for_status()
    dados = r.json()
    _token.update(valor=dados["access_token"], expira=time.time() + dados["expires_in"])
    return _token["valor"]


def enviar_email(destinatario: str, assunto: str, html: str):
    remetente = os.environ["MAIL_SENDER_UPN"]
    r = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{remetente}/sendMail",
        headers={"Authorization": f"Bearer {_obter_token()}"},
        json={
            "message": {
                "subject": assunto,
                "body": {"contentType": "HTML", "content": html},
                "toRecipients": [{"emailAddress": {"address": destinatario}}],
            },
            "saveToSentItems": False,
        },
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Graph sendMail {r.status_code}: {r.text[:300]}")
