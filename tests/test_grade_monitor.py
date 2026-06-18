from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import grade_monitor as gm


TRANSCRIPT_URL = "https://apps.guc.edu.eg/student_ext/Grade/Transcript_001.aspx?v=test"

INITIAL_HTML = """
<html>
  <body>
    <form method="post">
      <input type="hidden" name="__VIEWSTATE" value="view-state">
      <input type="hidden" name="__EVENTVALIDATION" value="event-validation">
      <select name="ctl00$ctl00$ContentPlaceHolderright$ContentPlaceHoldercontent$stdYrLst"
              id="ContentPlaceHolderright_ContentPlaceHoldercontent_stdYrLst">
        <option value="">Choose a study year</option>
        <option value="23">2025-2026</option>
        <option value="22" selected>2024-2025</option>
      </select>
    </form>
  </body>
</html>
"""

SELECTED_HTML = """
<html>
  <body>
    <h4>Student Portal - SIS</h4>
    <ul>
      <li>Main</li>
      <li>Evaluation</li>
      <li>Grade</li>
    </ul>
    <div>Transcript</div>
    <div>Choose Season:</div>
    <select>
      <option>2027-2028</option>
      <option>2026-2027</option>
      <option selected>2025-2026</option>
    </select>
    <div>Your Info:</div>
    <strong>Name:</strong> Test Student
    <strong>Year:</strong> 2025-2026
    <strong>Study Group:</strong> Engineering
    <div>Transcript:</div>
    <table>
      <tr><td><strong>Winter 2025</strong></td></tr>
      <tr><td><strong>Semester</strong></td><td><strong>Course Name</strong></td><td><strong>Numeric</strong></td><td><strong>Grade</strong></td><td><strong>Hours</strong></td></tr>
      <tr><td>CSE09</td><td>Machine Learning</td><td>2.3</td><td>B-</td><td>4</td></tr>
      <tr><td></td><td><strong>Semester GPA in Current Study Group</strong></td><td>2.3</td><td></td><td>4</td></tr>
    </table>
    <div>Current Cumulative GPA for Engineering including German Language 3.28</div>
    <h3>ShortCuts</h3>
  </body>
</html>
"""


def run_monitor_silently(force: bool = False) -> int:
    with redirect_stdout(StringIO()):
        return gm.run(force=force)


class GradeMonitorTests(unittest.TestCase):
    def test_bool_parsing(self) -> None:
        self.assertTrue(gm.parse_bool("true"))
        self.assertTrue(gm.parse_bool("YES"))
        self.assertFalse(gm.parse_bool("0"))
        self.assertFalse(gm.parse_bool(None))
        self.assertTrue(gm.parse_bool(None, default=True))

    def test_decode_response_body_detects_utf16_without_charset(self) -> None:
        html = "<html><body>Transcript</body></html>"
        self.assertEqual(gm.decode_response_body(html.encode("utf-16le")), html)

    def test_unpack_packed_javascript_extracts_cookie_and_location(self) -> None:
        script = (
            "eval(function(p,a,c,k,e,d){}"
            "('0.1=\"2=3\";4.5=\"6.aspx\"',7,7,"
            "'document|cookie|abc|xyz|location|href|next'.split('|'),0,{}))"
        )

        unpacked = "\n".join(gm.unpack_packed_javascript(script))

        self.assertIn('document.cookie="abc=xyz"', unpacked)
        self.assertEqual(gm.extract_javascript_cookie_names(unpacked), ["abc"])
        self.assertEqual(gm.extract_javascript_locations(unpacked), ["next.aspx"])

    def test_extract_transcript_v_arguments(self) -> None:
        self.assertEqual(gm.extract_transcript_v_arguments("sTo('SMP359651');sTo(c)"), ["SMP359651"])

    def test_transcript_redirect_challenge_urls(self) -> None:
        html = "<script>function sTo(c){};sTo('UQN278578')</script>"
        result = gm.FetchResult(gm.DEFAULT_TRANSCRIPT_URL, gm.DEFAULT_TRANSCRIPT_URL, 200, html)

        self.assertEqual(
            gm.transcript_redirect_challenge_urls(result),
            ["https://apps.guc.edu.eg/student_ext/Grade/Transcript_001.aspx?v=UQN278578"],
        )

    def test_check_window_and_friday_skip(self) -> None:
        with mock.patch.dict(os.environ, {"CHECK_START": "09:00", "CHECK_END": "17:30", "SKIP_DAYS": "friday"}, clear=True):
            cairo = ZoneInfo("Africa/Cairo")
            self.assertTrue(gm.within_check_window(datetime(2026, 6, 16, 9, 0, tzinfo=cairo)))
            self.assertTrue(gm.within_check_window(datetime(2026, 6, 16, 17, 30, tzinfo=cairo)))
            self.assertFalse(gm.within_check_window(datetime(2026, 6, 16, 18, 0, tzinfo=cairo)))
            self.assertFalse(gm.within_check_window(datetime(2026, 6, 19, 12, 0, tzinfo=cairo)))

    def test_force_check_overrides_window_and_skip_day(self) -> None:
        with mock.patch.dict(os.environ, {"FORCE_CHECK": "true", "SKIP_DAYS": "friday"}, clear=True):
            self.assertTrue(gm.within_check_window(datetime(2026, 6, 19, 3, 0, tzinfo=ZoneInfo("Africa/Cairo"))))

    def test_form_parser_finds_target_year_with_dash_or_slash(self) -> None:
        expected = ("ctl00$ctl00$ContentPlaceHolderright$ContentPlaceHoldercontent$stdYrLst", "23")
        self.assertEqual(gm.find_study_year_selection(INITIAL_HTML, "2025-2026"), expected)
        self.assertEqual(gm.find_study_year_selection(INITIAL_HTML, "2025/2026"), expected)

    def test_load_urls_defaults_to_generic_guc_transcript_url(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gm.load_urls(), [gm.DEFAULT_TRANSCRIPT_URL])

    def test_transcript_url_strips_generated_v_parameter(self) -> None:
        generated_url = "https://apps.guc.edu.eg/student_ext/Grade/Transcript_001.aspx?v=SMP359651"
        self.assertEqual(gm.canonicalize_transcript_url(generated_url), gm.DEFAULT_TRANSCRIPT_URL)

    def test_load_urls_preserves_generated_v_parameter_for_fetching(self) -> None:
        generated_url = "https://apps.guc.edu.eg/student_ext/Grade/Transcript_001.aspx?v=SMP359651"
        with mock.patch.dict(os.environ, {"TRANSCRIPT_URL": generated_url}, clear=True):
            self.assertEqual(gm.load_urls(), [generated_url])

    def test_generated_url_candidates_try_given_url_then_stable_url(self) -> None:
        generated_url = "https://apps.guc.edu.eg/student_ext/Grade/Transcript_001.aspx?v=SMP359651"
        self.assertEqual(gm.transcript_url_candidates(generated_url), [generated_url, gm.DEFAULT_TRANSCRIPT_URL])

    def test_transcript_url_with_generated_v_replaces_existing_v(self) -> None:
        generated_url = "https://apps.guc.edu.eg/student_ext/Grade/Transcript_001.aspx?v=OLD123456"
        self.assertEqual(
            gm.transcript_url_with_generated_v(generated_url, "UQN278578"),
            "https://apps.guc.edu.eg/student_ext/Grade/Transcript_001.aspx?v=UQN278578",
        )

    def test_transcript_url_keeps_non_v_query_parameters(self) -> None:
        url = "https://apps.guc.edu.eg/student_ext/Grade/Transcript_001.aspx?x=1&v=SMP359651"
        self.assertEqual(gm.canonicalize_transcript_url(url), f"{gm.DEFAULT_TRANSCRIPT_URL}?x=1")

    def test_non_transcript_url_is_not_changed(self) -> None:
        url = "https://apps.guc.edu.eg/student_ext/Other.aspx?v=SMP359651"
        self.assertEqual(gm.canonicalize_transcript_url(url), url)

    def test_workflow_commit_step_handles_new_state_file(self) -> None:
        workflow = Path(".github/workflows/check-grades.yml").read_text(encoding="utf-8")
        add_index = workflow.index("git add state/last_seen.json")
        diff_index = workflow.index("git diff --cached --quiet -- state/last_seen.json")

        self.assertLess(add_index, diff_index)
        self.assertIn("[ ! -f state/last_seen.json ]", workflow)

    def test_select_transcript_year_posts_aspnet_fields(self) -> None:
        result = gm.FetchResult(TRANSCRIPT_URL, TRANSCRIPT_URL, 200, INITIAL_HTML)

        with mock.patch.object(gm, "request_url") as request_url:
            request_url.return_value = gm.FetchResult(TRANSCRIPT_URL, TRANSCRIPT_URL, 200, SELECTED_HTML)
            selected = gm.select_transcript_year(result, "session-cookie", "2025-2026")

        self.assertEqual(selected.text, SELECTED_HTML)
        _, kwargs = request_url.call_args
        self.assertEqual(kwargs["method"], "POST")
        data = kwargs["data"]
        self.assertEqual(data["__VIEWSTATE"], "view-state")
        self.assertEqual(data["__EVENTVALIDATION"], "event-validation")
        self.assertEqual(data["__EVENTTARGET"], "ctl00$ctl00$ContentPlaceHolderright$ContentPlaceHoldercontent$stdYrLst")
        self.assertEqual(data["ctl00$ctl00$ContentPlaceHolderright$ContentPlaceHoldercontent$stdYrLst"], "23")

    def test_build_monitored_text_prefers_transcript_region(self) -> None:
        result = gm.FetchResult(TRANSCRIPT_URL, TRANSCRIPT_URL, 200, SELECTED_HTML)
        monitored = gm.build_monitored_text([result], "2025-2026")

        self.assertIn("--- Selected academic year 2025-2026 transcript ---", monitored)
        self.assertIn("Machine Learning", monitored)
        self.assertIn("Current Cumulative GPA", monitored)
        self.assertNotIn("Choose Season", monitored)
        self.assertNotIn("ShortCuts", monitored)

    def test_build_monitored_text_removes_volatile_footer_dates(self) -> None:
        dated_html = SELECTED_HTML.replace("<h3>ShortCuts</h3>", "<div>6/18/2026</div><h3>ShortCuts</h3>")
        result = gm.FetchResult(TRANSCRIPT_URL, TRANSCRIPT_URL, 200, dated_html)
        monitored = gm.build_monitored_text([result], "2025-2026")

        self.assertNotIn("6/18/2026", monitored)
        self.assertEqual(
            gm.signature(monitored),
            gm.signature(gm.build_monitored_text([gm.FetchResult(TRANSCRIPT_URL, TRANSCRIPT_URL, 200, SELECTED_HTML)], "2025-2026")),
        )

    def test_volatile_signature_line_detection(self) -> None:
        self.assertTrue(gm.line_is_volatile_for_signature("6/18/2026"))
        self.assertTrue(gm.line_is_volatile_for_signature("Printed on: 2026-06-18"))
        self.assertFalse(gm.line_is_volatile_for_signature("Spring 2026"))
        self.assertFalse(gm.line_is_volatile_for_signature("Artificial Intelligence"))

    def test_build_monitored_text_rejects_wrong_year_transcript_region(self) -> None:
        wrong_year_html = SELECTED_HTML.replace("Year:</strong> 2025-2026", "Year:</strong> 2024-2025")
        result = gm.FetchResult(TRANSCRIPT_URL, TRANSCRIPT_URL, 200, wrong_year_html)

        with self.assertRaisesRegex(gm.MonitorError, "does not mention the configured academic year"):
            gm.build_monitored_text([result], "2025-2026")

    def test_build_monitored_text_uses_stable_url_for_generated_url(self) -> None:
        result = gm.FetchResult(TRANSCRIPT_URL, TRANSCRIPT_URL, 200, SELECTED_HTML)
        monitored = gm.build_monitored_text([result], "2025-2026")

        self.assertIn(f"URL: {gm.DEFAULT_TRANSCRIPT_URL}", monitored)
        self.assertNotIn("?v=test", monitored)

    def test_build_monitored_text_rejects_empty_visible_page(self) -> None:
        result = gm.FetchResult(gm.DEFAULT_TRANSCRIPT_URL, gm.DEFAULT_TRANSCRIPT_URL, 200, "<html><body></body></html>")

        with self.assertRaisesRegex(gm.MonitorError, "No visible transcript text"):
            gm.build_monitored_text([result], "2025-2026")

    def test_build_monitored_text_rejects_unrelated_visible_page(self) -> None:
        html = "<html><body><h1>Student Portal</h1><div>Main</div><div>Evaluation</div></body></html>"
        result = gm.FetchResult(gm.DEFAULT_TRANSCRIPT_URL, gm.DEFAULT_TRANSCRIPT_URL, 200, html)

        with self.assertRaisesRegex(gm.MonitorError, "Could not find transcript content"):
            gm.build_monitored_text([result], "2025-2026")

    def test_build_monitored_text_allows_course_evaluation_request(self) -> None:
        html = """
        <html><body>
          <div>Please evaluate the course Advanced Databases before viewing the posted result.</div>
        </body></html>
        """
        result = gm.FetchResult(gm.DEFAULT_TRANSCRIPT_URL, gm.DEFAULT_TRANSCRIPT_URL, 200, html)
        monitored = gm.build_monitored_text([result], "2025-2026")

        self.assertIn("Possible course evaluation request", monitored)
        self.assertIn("Advanced Databases", monitored)

    def test_fetch_transcript_falls_back_to_stable_url_when_generated_url_is_empty(self) -> None:
        generated_url = "https://apps.guc.edu.eg/student_ext/Grade/Transcript_001.aspx?v=SMP359651"
        requested_urls = []

        def fake_request_url(url: str, cookie_header: str | None, **kwargs: object) -> gm.FetchResult:
            requested_urls.append(url)
            if url == generated_url:
                return gm.FetchResult(url, url, 200, "<html><body></body></html>")
            return gm.FetchResult(url, url, 200, SELECTED_HTML)

        with mock.patch.object(gm, "request_url", side_effect=fake_request_url):
            result = gm.fetch_transcript(generated_url, "session-cookie", "2025-2026")

        self.assertEqual(result.url, gm.DEFAULT_TRANSCRIPT_URL)
        self.assertEqual(requested_urls, [generated_url, gm.DEFAULT_TRANSCRIPT_URL])

    def test_fetch_transcript_follows_generated_url_challenge(self) -> None:
        challenge_html = "<script>function sTo(c){};sTo('UQN278578')</script>"
        generated_url = "https://apps.guc.edu.eg/student_ext/Grade/Transcript_001.aspx?v=UQN278578"
        requested_urls = []

        def fake_request_url(url: str, cookie_header: str | None, **kwargs: object) -> gm.FetchResult:
            requested_urls.append(url)
            if url == gm.DEFAULT_TRANSCRIPT_URL:
                return gm.FetchResult(url, url, 200, challenge_html)
            return gm.FetchResult(url, url, 200, SELECTED_HTML)

        with mock.patch.object(gm, "request_url", side_effect=fake_request_url):
            result = gm.fetch_transcript(gm.DEFAULT_TRANSCRIPT_URL, "session-cookie", "2025-2026")

        self.assertEqual(result.url, generated_url)
        self.assertEqual(requested_urls, [gm.DEFAULT_TRANSCRIPT_URL, generated_url])

    def test_fetch_transcript_rejects_all_empty_candidates(self) -> None:
        generated_url = "https://apps.guc.edu.eg/student_ext/Grade/Transcript_001.aspx?v=SMP359651"

        with mock.patch.object(
            gm,
            "request_url",
            return_value=gm.FetchResult(generated_url, generated_url, 200, "<html><body></body></html>"),
        ):
            with self.assertRaisesRegex(gm.MonitorError, "No transcript URL candidate"):
                gm.fetch_transcript(generated_url, "session-cookie", "2025-2026")

    def test_login_page_detection(self) -> None:
        result = gm.FetchResult(
            "https://example.test/login",
            "https://example.test/login",
            200,
            "<input type='password'>",
        )
        self.assertTrue(gm.looks_like_login_page(result, "Username\nPassword"))

    def test_state_round_trip_and_invalid_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "last_seen.json"
            data = {"signature": "abc", "urls": [TRANSCRIPT_URL]}
            gm.save_state(path, data)
            self.assertEqual(gm.load_state(path), data)

            path.write_text("not json", encoding="utf-8")
            with self.assertRaises(gm.MonitorError):
                gm.load_state(path)

    def test_email_excerpt_marks_truncation(self) -> None:
        text = "x" * (gm.MAX_EMAIL_BODY_CHARS + 7)
        excerpt = gm.email_excerpt(text)
        self.assertIn("[truncated 7 characters]", excerpt)
        self.assertLess(len(excerpt), len(text) + 50)

    def test_send_email_defaults_recipient_to_smtp_username(self) -> None:
        env = {
            "SMTP_USERNAME": "student@example.test",
            "SMTP_PASSWORD": "app-password",
        }
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(gm.smtplib, "SMTP_SSL") as smtp_ssl:
            smtp = smtp_ssl.return_value.__enter__.return_value
            gm.send_email("Subject", "Body")

        smtp.login.assert_called_once_with("student@example.test", "app-password")
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "student@example.test")
        self.assertEqual(message["From"], "student@example.test")

    def test_run_creates_baseline_without_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "TRANSCRIPT_URL": TRANSCRIPT_URL,
                "SESSION_COOKIE": "cookie",
                "STATE_FILE": str(Path(tmp) / "state.json"),
                "FORCE_CHECK": "true",
            }
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(gm, "fetch_transcript", return_value=gm.FetchResult(TRANSCRIPT_URL, TRANSCRIPT_URL, 200, SELECTED_HTML)), \
                 mock.patch.object(gm, "send_email") as send_email:
                self.assertEqual(run_monitor_silently(), 0)
                send_email.assert_not_called()
                self.assertTrue(Path(env["STATE_FILE"]).exists())

    def test_run_no_change_sends_no_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            monitored = gm.build_monitored_text([gm.FetchResult(TRANSCRIPT_URL, TRANSCRIPT_URL, 200, SELECTED_HTML)], "2025-2026")
            gm.save_state(state_file, {"signature": gm.signature(monitored), "monitor_state_version": gm.MONITOR_STATE_VERSION})
            env = {
                "TRANSCRIPT_URL": TRANSCRIPT_URL,
                "SESSION_COOKIE": "cookie",
                "STATE_FILE": str(state_file),
                "FORCE_CHECK": "true",
            }
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(gm, "fetch_transcript", return_value=gm.FetchResult(TRANSCRIPT_URL, TRANSCRIPT_URL, 200, SELECTED_HTML)), \
                 mock.patch.object(gm, "send_email") as send_email:
                self.assertEqual(run_monitor_silently(), 0)
                send_email.assert_not_called()

    def test_run_migrates_old_state_version_without_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            gm.save_state(state_file, {"signature": "old-format-signature"})
            env = {
                "TRANSCRIPT_URL": TRANSCRIPT_URL,
                "SESSION_COOKIE": "cookie",
                "STATE_FILE": str(state_file),
                "FORCE_CHECK": "true",
                "SMTP_USERNAME": "sender@example.test",
                "SMTP_PASSWORD": "app-password",
            }
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(gm, "fetch_transcript", return_value=gm.FetchResult(TRANSCRIPT_URL, TRANSCRIPT_URL, 200, SELECTED_HTML)), \
                 mock.patch.object(gm, "send_email") as send_email:
                self.assertEqual(run_monitor_silently(), 0)
                send_email.assert_not_called()

            saved = gm.load_state(state_file)
            self.assertEqual(saved["monitor_state_version"], gm.MONITOR_STATE_VERSION)
            self.assertNotEqual(saved["signature"], "old-format-signature")

    def test_run_change_sends_email_and_updates_state(self) -> None:
        changed_html = SELECTED_HTML.replace("B-", "A")
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            gm.save_state(state_file, {"signature": "old", "monitor_state_version": gm.MONITOR_STATE_VERSION})
            env = {
                "TRANSCRIPT_URL": TRANSCRIPT_URL,
                "SESSION_COOKIE": "cookie",
                "STATE_FILE": str(state_file),
                "FORCE_CHECK": "true",
                "SMTP_USERNAME": "sender@example.test",
                "SMTP_PASSWORD": "app-password",
            }
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(gm, "fetch_transcript", return_value=gm.FetchResult(TRANSCRIPT_URL, TRANSCRIPT_URL, 200, changed_html)), \
                 mock.patch.object(gm, "send_email") as send_email:
                self.assertEqual(run_monitor_silently(), 0)
                send_email.assert_called_once()

            saved = gm.load_state(state_file)
            self.assertNotEqual(saved["signature"], "old")

    def test_run_send_current_emails_snapshot_even_without_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "TRANSCRIPT_URL": TRANSCRIPT_URL,
                "SESSION_COOKIE": "cookie",
                "STATE_FILE": str(Path(tmp) / "state.json"),
                "FORCE_CHECK": "true",
                "SEND_CURRENT_TRANSCRIPT": "true",
                "SMTP_USERNAME": "sender@example.test",
                "SMTP_PASSWORD": "app-password",
            }
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(gm, "fetch_transcript", return_value=gm.FetchResult(TRANSCRIPT_URL, TRANSCRIPT_URL, 200, SELECTED_HTML)), \
                 mock.patch.object(gm, "send_email") as send_email:
                self.assertEqual(run_monitor_silently(), 0)
                send_email.assert_called_once()

    def test_run_send_current_does_not_save_state_if_email_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            env = {
                "TRANSCRIPT_URL": TRANSCRIPT_URL,
                "SESSION_COOKIE": "cookie",
                "STATE_FILE": str(state_file),
                "FORCE_CHECK": "true",
                "SEND_CURRENT_TRANSCRIPT": "true",
                "SMTP_USERNAME": "sender@example.test",
                "SMTP_PASSWORD": "app-password",
            }
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(gm, "fetch_transcript", return_value=gm.FetchResult(TRANSCRIPT_URL, TRANSCRIPT_URL, 200, SELECTED_HTML)), \
                 mock.patch.object(gm, "send_email", side_effect=gm.MonitorError("smtp failed")):
                with self.assertRaises(gm.MonitorError):
                    run_monitor_silently()

            self.assertFalse(state_file.exists())

    def test_allow_first_email_does_not_save_state_if_email_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            env = {
                "TRANSCRIPT_URL": TRANSCRIPT_URL,
                "SESSION_COOKIE": "cookie",
                "STATE_FILE": str(state_file),
                "FORCE_CHECK": "true",
                "ALLOW_FIRST_EMAIL": "true",
                "SMTP_USERNAME": "sender@example.test",
                "SMTP_PASSWORD": "app-password",
            }
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(gm, "fetch_transcript", return_value=gm.FetchResult(TRANSCRIPT_URL, TRANSCRIPT_URL, 200, SELECTED_HTML)), \
                 mock.patch.object(gm, "send_email", side_effect=gm.MonitorError("smtp failed")):
                with self.assertRaises(gm.MonitorError):
                    run_monitor_silently()

            self.assertFalse(state_file.exists())

    def test_run_requires_auth_configuration_before_fetching(self) -> None:
        with mock.patch.dict(os.environ, {"TRANSCRIPT_URL": TRANSCRIPT_URL, "FORCE_CHECK": "true"}, clear=True), \
             mock.patch.object(gm, "fetch_transcript") as fetch_transcript:
            with self.assertRaises(gm.MonitorError):
                run_monitor_silently()
            fetch_transcript.assert_not_called()

    def test_self_test_email_sends_and_skips_monitoring(self) -> None:
        with mock.patch.dict(os.environ, {"SMTP_USERNAME": "sender@example.test", "SMTP_PASSWORD": "app-password"}, clear=True), \
             mock.patch.object(gm, "send_email") as send_email, \
             redirect_stdout(StringIO()):
            self.assertEqual(gm.send_self_test_email(), 0)

        send_email.assert_called_once()
        subject = send_email.call_args.kwargs["subject"]
        body = send_email.call_args.kwargs["body"]
        self.assertIn("self-test", subject)
        self.assertIn("cloud email path is working", body)

    def test_main_notifies_auth_failures_when_smtp_configured(self) -> None:
        with mock.patch.dict(os.environ, {"SMTP_USERNAME": "sender@example.test", "SMTP_PASSWORD": "app-password"}, clear=True), \
             mock.patch.object(sys, "argv", ["grade_monitor.py"]), \
             mock.patch.object(gm, "run", side_effect=gm.AuthError("bad credentials")), \
             mock.patch.object(gm, "send_email") as send_email, \
             mock.patch("sys.stderr", new=StringIO()):
            self.assertEqual(gm.main(), 2)

        send_email.assert_called_once()
        subject = send_email.call_args.kwargs["subject"]
        body = send_email.call_args.kwargs["body"]
        self.assertIn("failed: auth", subject)
        self.assertIn("bad credentials", body)

    def test_main_skips_failure_email_when_smtp_is_missing(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(sys, "argv", ["grade_monitor.py"]), \
             mock.patch.object(gm, "run", side_effect=gm.MonitorError("broken")), \
             mock.patch.object(gm, "send_email") as send_email, \
             mock.patch("sys.stderr", new=StringIO()):
            self.assertEqual(gm.main(), 1)

        send_email.assert_not_called()


if __name__ == "__main__":
    unittest.main()
