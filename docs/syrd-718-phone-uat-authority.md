# SYRD-718: the User's UAT decisions from a phone, over Tailscale Serve

SYRD-714 sends the User a private link to a ticket, `https://<machine>.<tailnet>.ts.net:<port>/#ticket=<id>`, served by
this host's Tailscale Serve. On the phone the link opened the ticket and Ask Director worked, but the UAT panel offered
neither Accept nor Send to Director. The User accepted SYRD-714 from the desktop instead.

## Why the phone had no Accept

The UAT panel asks the board what the User may do (`POST /api/ui/delegations {describe}`). That route began with the
board UI's origin check, which accepts only `127.0.0.1`, `localhost` and `[::1]`. Through Serve, the browser's Host is the
tailnet name, so the board refused the description, and the panel said "Accept and Send to Director are unavailable".
Ask Director is fixed in the page, so it still worked.

The same requests also showed a wider gap. Over the tailnet, the page and `/api/client-config` handed the board's write
token to anyone who reached the board, and with it any write as the User:
- another tailnet user, for example with a copied link;
- a tagged device;
- a Funnel visitor, had Funnel ever been turned on.

## Who is on the other end of a tailnet request

Tailscale Serve proxies to the board's loopback port from tailscaled, which runs as root. The kernel names root, not the
User's account, so the desktop rule (the connection's uid is an approved board UI user's) cannot apply.

What tailscaled does add is the requester's identity. In `ipn/ipnlocal/serve.go` of the deployed 1.104.2,
`addTailscaleIdentityHeaders`:
1. deletes any `Tailscale-User-Login`, `-Name`, `-Profile-Pic`, `Tailscale-Funnel-Request` and `Tailscale-Headers-Info`
   header the client sent;
2. marks a Funnel request `Tailscale-Funnel-Request: ?1`;
3. sets the login only for an untagged tailnet peer that WhoIs names, never for a tagged node, a Funnel request or
   the host's own traffic.

`addProxyForwardedHeaders` sets `X-Forwarded-Host` (the browser's Host, which it also keeps as Host),
`X-Forwarded-Proto: https` and `X-Forwarded-For`, the peer's address.

So the header is tailscaled's word only on tailscaled's connection. The board bounds that provenance with the kernel: it
believes the header only on a loopback connection that `/proc/net/tcp` says root owns. No local account but root can own
one. Root is already trusted with the board's record, its unit and its database.

`ui_user_identity.tailnet_refusal` is `''` (the User) only when all of these hold:
- the connection's uid is in `TAILNET_PROXY_UIDS`, which is `{0}`; only a test that stands in its own proxy changes it;
- there is no `Tailscale-Funnel-Request`;
- Host names the page by its tailnet name (`*.ts.net`) or a tailnet address, with any port, so a name a DNS-rebinding
  site could give the request is refused;
- `X-Forwarded-Proto` is `https`: `tailscale serve --http` is not the User's, and over HTTPS the browser checks the
  certificate for the name;
- `Tailscale-User-Login` is a login root recorded beside an approved board UI user, in any case.

Nothing else tailscaled sets is checked, because nothing else can differ on its connection: it sets `X-Forwarded-Host`
from the same Host it keeps, `X-Forwarded-For` to a peer WhoIs named, and each header once.

## Where the login comes from

The login is recorded beside the desktop account in the project's root-owned record, the same `board_ui` entry and
the same trust rules as SYRD-598 (owner root, no symlink, not writable by others):

```
sudo switchyard upgrade <project> --board-ui-user eric=eric@example.com
```

- `--board-ui-user` keeps its plumbing through `new` and `upgrade`. `board_ui_record.operator_choice` splits
  `ACCOUNT[=LOGIN]`, and a login Tailscale would never name (no `@`, whitespace) is refused, leaving the record as it was.
- A later run keeps a login recorded for the same account and uid, including the default desktop-owner run that names
  nobody. `ACCOUNT=` removes it.
- The board refuses the whole record, as for any other malformed entry, when a login is not text or not login-shaped.

## What the User may do from the phone

`ui_delegation`:
- **Writes.** `user_channel_refusal` is `''` for a direct request, which is unchanged, and otherwise is the tailnet
  refusal. A request counts as proxied when it carries any proxy header (`Tailscale-*`, `X-Forwarded-*`, `Forwarded`) or
  names the board by anything but a loopback name. Every HTTP write calls `require_user_channel` before its token check,
  so the page says why it was refused rather than "invalid write token".
- **The write token.** The page (`/`) and `/api/client-config` hand out `page_write_token`, which is empty for a request
  that may not act as the User.
- **Origin.** `_same_origin` accepts a proxied page as `https://<Host>`. Every route that reaches it is a write that
  `require_user_channel` has already limited to the User's tailnet login. The loopback page is still `http://<Host>` on
  the board's own port.
- **Director operations.** `lending_refusal` lends nothing to a proxied request ("the board UI lends Director
  operations only on the desktop, never over the tailnet"). Describe, prepare, mint and spending a grant all go
  through it, so the control role's operations stay the desktop account's.

The User's own declared actions are posted directly, as on the desktop:
- Accept: `user_sign_off`;
- Send to Director: `user_send_to_director`, with its message;
- Ask Director.

The workflow allows each only from User Review, so a second tap, or a page left open after the ticket moved, is refused.

Unchanged:
- A desktop page on loopback: its token, its session cookie, and its kernel-UID lending.
- Role panes on the Unix socket. The check is skipped where `role_authority` is set.
- Root's own loopback clients, such as the notify bridge, which send no proxy header.

## Restart and stale tokens

A restarted board has a new write token. The page's existing SYRD-669 path does the rest:
1. its write is refused with 403;
2. it asks `/api/client-config`, which gives the approved login the new token;
3. it sends the write once more.

Another login is given none, so its retry stops there. The describe route needs no session, so a restart costs the phone
nothing more.

## Evidence

All tests use a production-built board, the shipped workflow and the real server. `tests/tailscale_serve_standin.py` is
a stand-in for tailscaled's Serve proxy, written from the 1.104.2 source quoted above.

- `tests/tailnet_user_authority_test.py`: main `a6a28fed` first, then this tree. Three accounts the kernel tells apart,
  using unshare subuids: the stand-in, the desktop account and another local account.
  - On main: the phone is described nothing, while a copied link's stranger, or a local account sending the headers,
    gets the write token and acts as the User.
  - Here:
    - the approved login gets the token and is described Accept and Send to Director, with no Director operation;
    - Accept, Send and Ask each happen once;
    - prepare and mint are refused over the tailnet;
    - another login, a tagged device, Funnel, the host's own traffic, a non-tailnet name, plain-HTTP Serve, a local
      account's spoofed headers (the desktop account's included), the tailnet name sent straight to the board, and a
      record without or with a malformed login are each refused whole, and the ticket is untouched;
    - a login recorded in another case is the same login;
    - the desktop's loopback lending is unchanged;
    - a page left open is refused;
    - after a restart, the stale token is refused, then renewed and retried once.
- `tests/tailnet_uat_browser_test.py`: Chromium at phone size opens the link through a TLS stand-in for a `*.ts.net`
  name, main first.
  - On main: Ask Director only.
  - Here:
    - Ask Director, Accept and Send to Director show;
    - Accept and Send to Director each happen once;
    - across a restart, the page's stale token is refused, renewed and Accept is done without a reload;
    - another login sees no Accept or Send to Director and holds no token.
- `tests/board_ui_record_test.py`: `ACCOUNT=LOGIN` is recorded, kept, not carried to another uid, refused when
  malformed, removed by `ACCOUNT=`, and read by the board.
- `tests/switchyard_parsers_boundary_test.py`: `new`'s and `upgrade`'s golden entries were edited by rule for the
  `ACCOUNT[=LOGIN]` metavar and the help's new last sentence.
- Mutants: 24, one at a time, each killed by these tests. They cover each identity check, the proxied rule, the token
  and write gates, the check order, the origin scheme, lending, the record writer's carry-forward and removal, and both
  login validations. Four checks no test could reach were removed rather than kept: `X-Forwarded-Host`,
  `X-Forwarded-For`, a duplicate-header guard and a Host port check.

Nothing live was changed. Recording the login, deploying and the phone UAT are the User's steps
(`docs/user-notifications.md`).
