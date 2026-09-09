# Host boundaries and failure diagnosis

The supported host is Codex with connected Chrome browser tools and normal NTULearn UI
access. Fresh collection requires supported UI/DOM observation and original-file download
capabilities. A standalone shell CLI cannot drive that host automatically; it can import
the host's validated private capture and perform all local retrieval independently.

Use only methods advertised by the current tools. Read-only DOM evaluation is not an
HTTP executor. Do not guess fetch/CDP/network APIs, inspect browser implementation code,
extract cookies, or switch channels around a known denial. Page assets and normal download
controls must be used according to their own documentation.

Distinguish failures before assigning a cause:

- A CLI configuration error means no source was configured, not a failed HTTP request.
- A tool-reported browser navigation error does not establish an HTTP status or identify
  an administrator policy. Record the operation, wrapper/source and observed response.
- A renderer crash may explain an accessibility/DOM timeout. Inspect the visible crash;
  one ordinary reload can test that specific hypothesis. Do not loop on an unchanged error.
- A login/MFA screen needs the user to complete that screen. Do not send credentials to
  another destination or ask the user to paste them into chat.
- An explicit permission or policy denial stops that path. Explain the exact supported
  permission/approval required; do not disable protections or improvise another transport.

If blocked, finish independent local work and save its state. Ask for only the smallest
currently necessary action, giving where, what, why and the visible completion signal.
Do not claim background continuation after ending the turn.

Observe only course content, related announcements and assessment descriptions/due labels.
Do not start attempts, submit, grade, post, message, change completion controls, visit
rosters or enumerate unrelated identities. Preserve unavailable items as unavailable;
normal UI metadata visibility does not authorize opening restricted bodies.
