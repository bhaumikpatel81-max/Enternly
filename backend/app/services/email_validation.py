"""
Email validation shared by every path that creates a candidate, vendor, or
staff account (career-site apply, vendor CV submit, admin add-user, vendor
add-login). Rejects malformed addresses and well-known placeholder/example
domains so test data typed into a form can never create a real account or
trigger a real invite email.
"""
import re

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# RFC 2606 reserved domains plus the placeholder domains people commonly type
# into forms while testing (often copied straight from the field's own
# placeholder hint, e.g. "priya@example.com" or "poc@acmestaffing.com").
# Not an exhaustive disposable-email list — just addresses that look like a
# real person but can never be delivered.
_BLOCKED_DOMAINS = {
    "example.com", "example.org", "example.net", "example.edu",
    "test.com", "testing.com", "sample.com", "samplemail.com",
    "acme.com", "acmestaffing.com", "acmecorp.com",
    "domain.com", "yourcompany.com", "company.com", "mycompany.com",
    "foo.com", "foobar.com", "dummy.com", "mydomain.com",
    "invalid", "localhost", "test",
}


def assert_real_email(email: str, field: str = "email") -> str:
    """
    Validate an email address before it is used to create an account or
    send an invite. Returns the normalised (stripped, lower-cased) address.
    Raises ValueError with a user-facing message if invalid.
    """
    normalized = (email or "").strip().lower()
    if not _EMAIL_RE.match(normalized):
        raise ValueError(f"'{email}' is not a valid {field} address.")
    domain = normalized.rsplit("@", 1)[-1]
    if domain in _BLOCKED_DOMAINS:
        raise ValueError(
            f"'{email}' looks like a placeholder/test address ({domain}) "
            "and can't receive mail. Enter a real email address."
        )
    return normalized
