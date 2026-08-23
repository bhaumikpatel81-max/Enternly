"""
Shared branded email base layout (navy hero, 2x2 detail card, optional
"What Happens Next" steps, optional CTA button, optional raw extra body).

This is a straight extraction of scheduling_api._build_hm_availability_html
(the first, hand-written version of this template) into a parameterized
function -- same inline CSS/table markup and Outlook <!--[if mso]--> VML
button fallback, verbatim, so refactoring existing callers onto this causes
zero visual regression. New branded emails should call build_branded_email()
directly instead of hand-rolling another copy of this HTML.

Every caller-supplied string is escaped internally EXCEPT hero_title_html
and extra_body_html, which are raw-HTML-by-design (a short "Foo<br>Bar."
headline, and a pre-built block of already-escaped markup respectively) --
callers passing user data into those two must escape it themselves.
"""
import html


def _esc(s) -> str:
    return html.escape(str(s or ""))


def _logo_url() -> str:
    """Absolute URL for the branded logo PNG served from this app's own
    /assets static mount (frontend/assets/Enternly_logo.png), so it loads
    reliably in every mail client instead of depending on an externally
    hosted SVG (Outlook has no/unreliable SVG support in HTML email --
    that logo used to render blank or half-drawn there)."""
    from .connectors import _load_email_cfg

    base = (_load_email_cfg().get("base_url") or "").strip().rstrip("/")
    if not base or any(x in base for x in ("localhost", "127.0.0.1", "0.0.0.0")):
        # TODO: set APP_BASE_URL in .env.prod to your real production domain --
        # this placeholder is not a live address.
        base = "https://your-enternly-domain.example"
    return f"{base}/assets/Enternly_logo.png"


def _esc_multiline(s) -> str:
    """Escape then turn newlines into <br> -- for admin-editable template
    bodies and other free-form text that relies on line breaks to be legible."""
    return _esc(s).replace("\n", "<br>")


def _cta_html(cta_label: str | None, cta_link: str | None) -> str:
    """Renders nothing when either half is missing -- a pure-notification
    email (no action link) must not get an invented button."""
    if not (cta_label and cta_link):
        return ""
    return f"""
          <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:8px">
            <tr><td align="center" style="padding-bottom:30px">
              <!--[if mso]>
              <v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word"
                href="{cta_link}" style="height:54px;v-text-anchor:middle;width:290px;" arcsize="12%"
                stroke="f" fillcolor="#2563EB">
                <w:anchorlock/>
                <center style="color:#ffffff;font-family:Arial,Helvetica,sans-serif;font-size:17px;font-weight:bold;">
                  {_esc(cta_label)}
                </center>
              </v:roundrect>
              <![endif]-->
              <!--[if !mso]><!-->
              <table cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td bgcolor="#2563EB" style="background-color:#2563EB;border-radius:10px">
                    <a href="{cta_link}" style="display:block;color:#ffffff;padding:17px 46px;text-decoration:none;font-size:16px;font-weight:700;font-family:Arial,Helvetica,sans-serif;letter-spacing:0.3px;text-align:center;border-radius:10px">
                      {_esc(cta_label)}
                    </a>
                  </td>
                </tr>
              </table>
              <!--<![endif]-->
            </td></tr>
            <tr><td align="center" style="padding-bottom:8px">
              <p style="font-size:12px;color:#9b9893;font-family:Arial,Helvetica,sans-serif;margin:0 0 5px 0">Or copy and paste this link in your browser:</p>
              <a href="{cta_link}" style="font-size:11px;color:#2563EB;font-family:Arial,Helvetica,sans-serif;word-break:break-all;text-decoration:underline">{cta_link}</a>
            </td></tr>
          </table>"""


def build_branded_email(
    *,
    eyebrow: str,
    hero_title_html: str,
    hero_subtitle: str,
    hero_footer_label: str | None = None,
    hero_footer_value: str | None = None,
    detail_cells: list[tuple[str, str]] | None = None,
    steps: list[tuple[str, str, str]] | None = None,
    about_text: str | None = None,
    about_heading: str | None = "About This Step",
    extra_body_html: str | None = None,
    cta_label: str | None = None,
    cta_link: str | None = None,
    footer_note: str = "Questions? Simply reply to this email and our hiring team will be happy to help.",
) -> str:
    logo_url = _logo_url()

    hero_footer_html = ""
    if hero_footer_label:
        hero_footer_html = f"""
          <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:26px;border-top:1px solid rgba(255,255,255,.15)">
            <tr><td style="padding-top:20px">
              <p style="font-size:19px;font-weight:700;color:#ffffff;font-family:Arial,Helvetica,sans-serif;margin:0 0 5px 0">{_esc(hero_footer_label)}</p>
              <p style="font-size:13px;color:#5FB4FF;font-family:Arial,Helvetica,sans-serif;margin:0">{_esc(hero_footer_value)}</p>
            </td></tr>
          </table>"""

    # Detail cells render as a 2-column grid, 2 rows per pair -- same shape
    # as the original hand-written HM template (up to 4 cells expected).
    detail_cells = detail_cells or []
    cell_rows = []
    for i in range(0, len(detail_cells), 2):
        pair = detail_cells[i:i + 2]
        cells_html = ""
        for j, (label, value) in enumerate(pair):
            pad = "padding:22px 12px 12px 24px" if i == 0 and j == 0 else \
                  "padding:22px 24px 12px 12px" if i == 0 and j == 1 else \
                  "padding:12px 12px 22px 24px" if j == 0 else \
                  "padding:12px 24px 22px 12px"
            cells_html += f"""
              <td width="50%" style="{pad}">
                <p style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#7b8498;font-family:Arial,Helvetica,sans-serif;margin:0">{_esc(label)}</p>
                <p style="font-size:17px;font-weight:700;color:#111827;font-family:Arial,Helvetica,sans-serif;margin:6px 0 0 0">{_esc(value)}</p>
              </td>"""
        cell_rows.append(f"<tr>{cells_html}</tr>")
    detail_card_html = f"""
          <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid #e7ebf4;border-radius:14px;margin-bottom:34px">
            {''.join(cell_rows)}
          </table>""" if detail_cells else ""

    # Step dot states: 'done' (filled blue -- already happened), 'current'
    # (filled green -- the step this email is about), 'pending' (hollow
    # outline -- not yet reached). Unrecognised/omitted state falls back to
    # 'pending' rather than raising, since a bad state is cosmetic, not fatal.
    _DOT_HTML = {
        "done":    '<span style="display:inline-block;width:11px;height:11px;border-radius:50%;background-color:#2563EB;font-size:0;line-height:0">&nbsp;</span>',
        "current": '<span style="display:inline-block;width:11px;height:11px;border-radius:50%;background-color:#16a34a;font-size:0;line-height:0">&nbsp;</span>',
        "pending": '<span style="display:inline-block;width:7px;height:7px;border-radius:50%;border:2px solid #bfc8de;font-size:0;line-height:0">&nbsp;</span>',
    }
    steps_html = ""
    if steps:
        rows = []
        for title, desc, dot_state in steps:
            dot = _DOT_HTML.get(dot_state, _DOT_HTML["pending"])
            desc_html = f'<br><span style="font-size:13px;color:#6b7280">{_esc(desc)}</span>' if desc else ""
            rows.append(f"""
            <tr>
              <td width="18" valign="top" style="padding-top:2px">{dot}</td>
              <td style="padding-bottom:20px;font-family:Arial,Helvetica,sans-serif">
                <strong style="font-size:14px;color:#111827">{_esc(title)}</strong>{desc_html}
              </td>
            </tr>""")
        steps_html = f"""
          <p style="font-size:20px;font-weight:700;color:#111827;font-family:Arial,Helvetica,sans-serif;margin:0 0 20px 0">What Happens Next</p>
          <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px">
            {''.join(rows)}
          </table>"""

    about_html = ""
    if about_text:
        heading_html = (
            f'<p style="font-size:20px;font-weight:700;color:#111827;font-family:Arial,Helvetica,sans-serif;margin:0 0 20px 0">{_esc(about_heading)}</p>'
            if about_heading else ""
        )
        about_html = f"""
          {heading_html}
          <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f8faff;border-radius:12px;margin-bottom:30px">
            <tr><td style="padding:22px;font-size:14px;line-height:1.8;color:#4b5563;font-family:Arial,Helvetica,sans-serif">
              {_esc_multiline(about_text)}
            </td></tr>
          </table>"""

    return f"""<!DOCTYPE html>
<html lang="en" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="X-UA-Compatible" content="IE=edge">
  <!--[if mso]>
  <xml><o:OfficeDocumentSettings><o:AllowPNG/><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml>
  <![endif]-->
  <style>
    body, table, td {{ margin:0; padding:0; }}
    @media only screen and (max-width:600px) {{
      .wrap {{ width:100% !important; }}
    }}
  </style>
</head>
<body style="margin:0;padding:0;background-color:#eef2ff;-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%">
<table width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#eef2ff" style="background-color:#eef2ff">
  <tr><td align="center" style="padding:32px 12px">

    <table class="wrap" width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%">

      <!-- ── HERO ── -->
      <tr>
        <td bgcolor="#0A1F44" style="background-color:#0A1F44;border-radius:16px 16px 0 0;padding:44px 44px 40px 44px">

          <table width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr><td align="center" style="padding-bottom:16px">
              <img src="{logo_url}" width="230" alt="Enternly" style="display:block;border:0;outline:none;height:auto;width:230px;max-width:230px">
            </td></tr>
            <tr><td align="center" style="padding-bottom:28px">
              <span style="font-size:11px;letter-spacing:2.5px;font-weight:700;text-transform:uppercase;color:#5FB4FF;font-family:Arial,Helvetica,sans-serif">{_esc(eyebrow)}</span>
            </td></tr>
          </table>

          <p style="font-size:36px;line-height:1.2;color:#ffffff;font-family:Arial,Helvetica,sans-serif;font-weight:800;margin:0 0 14px 0">{hero_title_html}</p>
          <p style="font-size:15px;line-height:1.7;color:#d6ddff;font-family:Arial,Helvetica,sans-serif;margin:0">{_esc(hero_subtitle)}</p>
          {hero_footer_html}

        </td>
      </tr>

      <!-- ── BODY ── -->
      <tr>
        <td bgcolor="#ffffff" style="background-color:#ffffff;padding:40px 44px 8px 44px">

          {detail_card_html}
          {steps_html}
          {about_html}
          {extra_body_html or ""}
          {_cta_html(cta_label, cta_link)}

        </td>
      </tr>

      <!-- ── FOOTER ── -->
      <tr>
        <td bgcolor="#fafbfe" style="background-color:#fafbfe;padding:26px 44px;border-top:1px solid #edf1f8;border-radius:0 0 16px 16px;text-align:center">
          <p style="font-size:12px;color:#6b7280;font-family:Arial,Helvetica,sans-serif;margin:0;line-height:1.8">
            {_esc(footer_note)}<br><br>
            <strong style="color:#0A1F44">Enternly</strong> &#183; Application Tracking System<br>
            This is an automated message &#8212; please do not reply directly to this address.
          </p>
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""
