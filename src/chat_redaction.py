"""The chat-body redaction CHAIN, owned in one place.

Two filters have to run, in this order, and neither is sufficient alone:

  1. ``filter_chat_secrets`` recognises secrets by PROVIDER SIGNATURE — a
     Telegram bot token's ``\\d+:AA…`` shape, an OpenAI ``sk-…``. It is blind to
     ``vault set KEY <value>`` whose value carries no signature, at ANY length.
  2. ``redact_vault_commands`` recognises the vault-set GRAMMAR by position, so
     it catches the value regardless of what it looks like.

The write path (``discord-bridge``) composed both inline; the room-ops reader
applied only (1). A ``vault set MY_API_KEY hunter2secret`` was therefore removed
on the way in and returned verbatim on the way out. Measured, not inferred::

    filter_chat_secrets('vault set K hunter2')     -> unchanged
    filter_chat_secrets('vault set K ' + 'a'*40)   -> unchanged
    filter_chat_secrets('vault set K 801…:AAF…')   -> [REDACTED-Telegram Bot Token]

Note the middle line: LENGTH is not the discriminator, signature is. A test that
reaches for a "long" value to prove the gap closed will pass on the old code.
"""
from __future__ import annotations


# Eager, not lazy: the reader picks its redactor once and degrades if this module
# will not import — a lazy import defers that past the ladder built to catch it.
from chat_secret_filter import filter_chat_secrets
from vault_intercept import redact_vault_commands


def redact_chat_body(text: str) -> str:
    """Vault-set grammar FIRST, then the signature filter.

    The write path composed these the other way round, which mangles a body both
    of them match — the grammar eats into the signature placeholder::

        filter->grammar: vault set TELEGRAM_BOT_TOKEN [vault: … ignored] Bot Token]23
        grammar->filter: vault set TELEGRAM_BOT_TOKEN [vault: … ignored]

    Both orders agree when only one filter matches, so this is strictly better.
    """
    return filter_chat_secrets(redact_vault_commands(text)).text
