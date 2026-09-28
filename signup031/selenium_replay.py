"""Compare bounded original observations after captured Selenium DOM events."""
import time
from signup031.manual_replay import _locate


def restore_selenium(page, record, *, progress=None, raise_errors=False):
    progress=progress or (lambda **event:None)
    limits = list(record['limitations'])
    try:
        page.goto(record['start_url'], wait_until='domcontentloaded', timeout=10000)
        for index,row in enumerate(record['actions'],1):
            progress(phase='action',stopped_action=index)
            element = _locate(page, row)
            if row['action'] == 'fill':
                element.fill(row['value'], timeout=5000)
            else:
                element.click(timeout=5000)
            progress(completed_actions=index)
        progress(phase='observations',stopped_action=None)
        deadline = time.monotonic() + 5
        while True:
            differences = []
            if page.url != record['final_url']:
                differences.append('saved URL differs')
            for row in record['observed']['inputs']:
                if _locate(page, row).input_value(timeout=1000) != row['value']:
                    differences.append('saved input differs: ' + row['selector'])
            if page.inner_text('body') != record['observed']['text']:
                differences.append('saved visible text differs')
            if not differences or time.monotonic() >= deadline:
                return limits + differences
            page.wait_for_timeout(50)
    except Exception as exc:
        if raise_errors:raise
        return limits + ['Selenium replay incomplete: ' + type(exc).__name__ + ': ' + str(exc)[:700]]
