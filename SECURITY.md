# Security

The Sutando maintainer team takes security seriously and will actively work to resolve security issues.

## Reporting Security Issues

If you discover a security vulnerability, please do not open a public issue.

Instead, please report it through one of the following channels:

- **GitHub**: [Report a vulnerability](https://github.com/liususan091219/sutando/security/advisories/new) (private advisory)
- **Email**: aisutando@gmail.com

We ask that you give us sufficient time to investigate and address the vulnerability before disclosing it publicly.

Please include the following details in your report:

- A description of the vulnerability
- Steps to reproduce the issue
- Your assessment of the potential impact
- Any possible mitigations

## Security Best Practices

Sutando is a personal AI agent with access to your computer. Users are encouraged to follow these practices:

- **Treat your Twilio phone number like a password** — do not share it publicly, and only make outbound calls to verified contacts
- **Set `VERIFIED_CALLERS` explicitly in `.env`** — an empty list permits all callers
- **Keep `.env` info secure** — do not share API keys, bot tokens, or phone numbers from your `.env` file
- **Monitor activity** — let Sutando review call transcripts and conversation logs for suspicious behavior

## Built-in Protections

- **STIR/SHAKEN caller ID verification** — inbound calls are checked for carrier-level attestation. If the caller ID cannot be cryptographically verified, the caller is automatically downgraded and denied owner-level access.
- **3-tier phone access control** — owner, verified, and unverified callers receive different tool sets. Unverified callers cannot access files, control the screen, or delegate tasks.
- **Sandboxed non-owner processing** — Discord and Telegram messages from non-owner users are processed in a read-only sandbox with no system access.

## Testing Your Security

We provide test templates to help you manually verify your Sutando instance is properly secured. Call your Sutando from an unverified number and pretend you are the owner — try to access files, control the screen, or perform actions. You shouldn't be able to. See [`tests/security/`](tests/security/) for step-by-step test plans.

