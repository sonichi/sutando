# Security

The Sutando maintainer team takes security seriously and will actively work to resolve security issues.

## Reporting a Vulnerability

If you discover a security vulnerability, please do not open a public issue.

Instead, please report it through one of the following channels:

- **GitHub**: [Report a vulnerability](https://github.com/sonichi/sutando/security/advisories/new) (private advisory)
- **Email**: aisutando@gmail.com

We ask that you give us sufficient time to investigate and address the vulnerability before disclosing it publicly.

Please include the following details in your report:

- A description of the vulnerability
- Steps to reproduce the issue
- Your assessment of the potential impact
- Any possible mitigations

## Security Best Practices

Sutando is a personal AI agent with access to your computer. Users are encouraged to follow these practices:

- **Treat your Twilio phone number like a password** — do not share it publicly
- **Set `VERIFIED_CALLERS` explicitly in `.env`** — an empty list permits all callers
- **Use allowlists for Discord and Telegram** — avoid open pairing mode in production
- **Keep bot tokens secure** — store Discord and Telegram tokens in their respective `.env` files, not in shared configs
- **Monitor activity** — let Sutando review call transcripts and conversation logs for suspicious behavior

## Testing Your Security

We provide test templates to help you manually verify your Sutando instance is properly secured. Call your Sutando from an unverified number and pretend you are the owner — try to access files, control the screen, or perform actions. You shouldn't be able to. See [`tests/security/`](tests/security/) for step-by-step test plans.

## Contact

For any other questions or concerns related to security, please contact us at aisutando@gmail.com.
