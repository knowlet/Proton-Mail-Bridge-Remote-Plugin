# Read-mail FETCH literal handling

Base: b84554f712d0680b63475e96dd62fdc31c9d45d0.

The single-message reader assumed the first imaplib FETCH item held the message
literal. A preceding unsolicited FLAGS response instead caused it to parse empty
bytes and return a successful empty email. A successful FETCH response containing
only nonliteral data had the same misleading result.

Walk response items to find a bytes/bytearray literal tuple, skipping nonliteral
items. If no literal exists, fail through the existing sanitized plugin error
path. An actual zero-length literal remains distinguishable from missing data.
This retains readonly SELECT and BODY.PEEK[], and changes neither header-list
fetches nor MIME extraction (separate open PRs #3 and #4).

References:
- Python imaplib FETCH response representation:
  https://docs.python.org/3/library/imaplib.html#imaplib.IMAP4.fetch
- IMAP unsolicited FETCH responses:
  https://www.rfc-editor.org/rfc/rfc9051.html#section-7.5.2

Offline regressions call the published plugin reader using synthetic IMAP
responses. On unchanged code, two missing-literal cases falsely report success,
and a FLAGS-before-body response loses the subject/body. After the fix these
cases behave correctly. Valid literal-first and actual empty-message controls
remain successful. Each case verifies readonly SELECT and BODY.PEEK[].
No mail account, Keychain, certificate, or live Bridge access is used.
