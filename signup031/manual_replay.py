"""Replay captured actions and compare a bounded set of saved observations."""


def _locate(page, row, *, timeout=5000):
    from playwright.sync_api import TimeoutError
    field = page.locator(row['selector'])
    if field.count() == 0:
        try:
            field.wait_for(state='attached', timeout=timeout)
        except TimeoutError:
            raise ValueError('recorded target missing or ambiguous: ' + row['selector'] +
                             f' (not attached within {timeout} ms)') from None
    if field.count() != 1:
        raise ValueError('recorded target missing or ambiguous: ' + row['selector'])
    signature = field.evaluate("e => ({tag:e.tagName,type:e.getAttribute('type')||'',name:e.getAttribute('name')||''})")
    if signature != row['signature']:
        raise ValueError('recorded target identity changed: ' + row['selector'])
    return field


def restore_manual(page, record, *, progress=None):
    progress=progress or (lambda **event:None)
    page.goto(record['start_url'], wait_until='domcontentloaded', timeout=30000)
    for index,row in enumerate(record['actions'],1):
        progress(phase='action',stopped_action=index)
        field = _locate(page, row)
        if row['action'] == 'fill':
            field.fill(row['value'], timeout=5000)
        else:
            field.click(timeout=5000)
        progress(completed_actions=index)
    progress(phase='observations',stopped_action=None)
    differences = []
    if page.url != record['final_url']:
        differences.append('saved URL differs')
    for row in record['observed']['inputs']:
        if _locate(page, row).input_value(timeout=5000) != row['value']:
            differences.append('saved input differs: ' + row['selector'])
    if page.inner_text('body') != record['observed']['text']:
        differences.append('saved visible text differs')
    if differences and not record['limitations']:
        raise ValueError('; '.join(differences))
    return record['limitations'] + differences
