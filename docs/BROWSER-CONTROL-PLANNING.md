# Browser Control (Future Planning)

Not currently enabled. When the agent needs to interact with authenticated websites (shopping, booking, admin dashboards, etc.), there are three viable approaches with different security tradeoffs.

## Option B: Cookie Sync to VPS Headless Chrome

**How it works**: Run headless Chromium on the VPS. Periodically export cookies for specific domains from your Mac's Chrome and sync them to the VPS browser profile.

| Pros | Cons |
|------|------|
| Agent stays contained on VPS | Cookies expire, need periodic re-sync |
| No Mac exposure | Fragile (Chrome cookie DB format can change) |
| Selective — only domains you choose | Requires scripting and maintenance |

**Implementation**: Script extracts cookies from Mac Chrome SQLite DB (`~/Library/Application Support/Google/Chrome/Default/Cookies`) for allowlisted domains, transfers via scp, imports into headless Chrome profile on VPS.

**Best for**: Low-frequency tasks on a small number of sites where sessions are long-lived.

## Option D: Password Vault with Dedicated Email

**How it works**: Give the agent access to a dedicated password vault (1Password, Bitwarden) containing only approved credentials. Agent logs in fresh on VPS headless Chrome.

| Pros | Cons |
|------|------|
| Clean separation — you choose exactly which accounts | 2FA and passwordless login are a problem |
| No Mac exposure | Agent must handle login flows |
| Auditable — vault logs show what was accessed | More setup per site |

**The 2FA/passwordless problem**: Most sites now use email magic links or SMS codes. Solutions:
- **Dedicated email** (e.g., `agent@yourdomain.com`) that only receives login codes, with the agent having IMAP access. Limited blast radius — this email has no other accounts attached.
- **TOTP-based 2FA**: Share the TOTP secret with the agent. Works for sites that support authenticator apps.
- **SMS forwarding**: Forward codes from a dedicated phone number to the agent via Telegram.

**Best for**: Sites with stable login flows and TOTP support. The dedicated email approach extends this to passwordless sites.

## Option E: Mac as Node with Restricted Browser Profile (Recommended)

**How it works**: Pair your Mac as an OpenClaw node, but restrict it to a dedicated Chrome profile with only specific logins, no shell access, and a website allowlist.

| Pros | Cons |
|------|------|
| Most practical — real browser, real logins | Mac is exposed (though restricted) |
| Website allowlist prevents malicious navigation | Requires Mac to be online |
| Exec denial prevents shell access | Dedicated profile needs separate login sessions |

**Key restrictions to apply**:
1. **Dedicated Chrome profile** — not your main profile. Only log into sites the agent should access.
2. **Exec approvals: "deny"** — agent can control the browser but cannot run terminal commands on your Mac (Settings -> Exec approvals).
3. **Website allowlist** — restrict which domains the agent can navigate to. Prevents prompt injection from steering the browser to malicious sites.
4. **Isolate from infrastructure** — keep Pulumi state and infra credentials off the Mac (or in a separate user account) to prevent self-modification attacks.

**Best for**: Most use cases. Balances convenience with security. The website allowlist is the critical control — it limits blast radius even if the agent is prompt-injected.

## Recommendation

Start with **Option E** for general-purpose browser tasks. The dedicated profile + exec deny + website allowlist combination provides a practical security boundary. Add **Option D** for headless automation of specific sites where you don't want Mac exposure at all.

Avoid giving the agent access to your main browser profile or unrestricted shell access on your Mac.
