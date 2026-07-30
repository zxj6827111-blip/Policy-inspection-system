import re
from pathlib import Path


def test_frontend_recovers_csrf_from_cookie_for_write_requests():
    js = Path("app/static/app.js").read_text(encoding="utf-8")

    assert "function readCookie" in js
    assert "readCookie('pis_csrf')" in js
    assert "headers['X-CSRF-Token']" in js


def test_hidden_attribute_wins_over_component_display_styles():
    css = Path("app/static/app.css").read_text(encoding="utf-8")

    assert "[hidden]" in css
    assert "display: none !important" in css


def test_evidence_fields_are_inside_dialog_body_and_values_are_visible():
    html = Path("app/templates/index.html").read_text(encoding="utf-8")
    dialog_start = html.index('<dialog id="evidence-dialog"')
    dialog_end = html.index("</dialog>", dialog_start)
    dialog = html[dialog_start:dialog_end]
    body_start = dialog.index('<div class="dlg-body">')
    fields_start = dialog.index('<div id="evidence-fields"')

    assert fields_start > body_start
    assert '<dd id="evidence-page-value">-</dd>' in dialog
    assert '<dd id="evidence-body-value">-</dd>' in dialog


def test_sidebar_group_labels_do_not_present_menu_counts_as_data_counts():
    html = Path("app/templates/index.html").read_text(encoding="utf-8")
    js = Path("app/static/app.js").read_text(encoding="utf-8")

    assert 'class="g-count"' not in html
    assert 'class="g-count"' not in js
    assert 'data-api="/api/review-queue"' in html
    assert 'data-api="/api/scan-exceptions"' in html


def test_anonymous_frontend_does_not_start_protected_polling():
    js = Path("app/static/app.js").read_text(encoding="utf-8")

    assert "setInterval" not in js
    assert "if (authState !== 'authenticated') return Promise.resolve()" in js
    assert "function handleUnauthorized" in js
    assert "stopPolling()" in js
    assert "document.hidden" in js


def test_login_and_operational_views_have_dedicated_components():
    html = Path("app/templates/index.html").read_text(encoding="utf-8")
    js = Path("app/static/app.js").read_text(encoding="utf-8")
    css = Path("app/static/app.css").read_text(encoding="utf-8")

    assert 'class="login-dialog"' in html
    assert 'id="login-password-toggle"' in html
    assert "function renderScanJobsView" in js
    assert "function renderProblemsView" in js
    assert ".business-table" in css
    assert ".issue-row" in css


def test_problem_navigation_uses_review_queue_buckets():
    html = Path("app/templates/index.html").read_text(encoding="utf-8")

    assert 'data-api="/api/review-queue?review_status=customer_formal"' in html
    assert 'data-api="/api/review-queue?review_status=insufficient_data"' in html
    assert 'data-api="/api/review-queue?review_status=auto_excluded"' in html
    assert 'data-api="/api/review-queue?review_status=human_excluded"' in html


def test_overview_cards_align_and_recent_jobs_are_collapsible():
    js = Path("app/static/app.js").read_text(encoding="utf-8")
    css = Path("app/static/app.css").read_text(encoding="utf-8")

    assert 'class="card recent-jobs-panel"' in js
    assert '<summary class="panel-head recent-jobs-summary">' in js
    assert "?.open ?? false" in js
    assert ".g-stat {" in css
    assert "margin-top: 0 !important" in css


def test_scan_jobs_expose_guarded_delete_control():
    html = Path("app/templates/index.html").read_text(encoding="utf-8")
    js = Path("app/static/app.js").read_text(encoding="utf-8")

    assert 'id="confirm-delete-job"' in html
    assert "scan-job-delete" in js
    assert "method: 'DELETE'" in js
    assert "['pending', 'running', 'cooling']" in js


def test_excel_download_surfaces_generation_state_and_uses_blob_download():
    js = Path("app/static/app.js").read_text(encoding="utf-8")

    assert "function downloadJobExport" in js
    assert "job-export" in js
    assert "生成中…" in js
    assert "response.blob()" in js
    direct_export_links = re.findall(
        r'<a class="([^"]*)" href="/api/jobs/\$\{job\.id\}/export"',
        js,
    )
    assert direct_export_links
    assert all("job-export" in classes.split() for classes in direct_export_links)
