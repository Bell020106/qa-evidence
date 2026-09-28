"""Common web scenario runner; a separate process for responsive desktop use."""
import argparse
import json
from pathlib import Path
import sys

from signup031.archive import finish_scenario_archive
from signup031.contract import utc_now
from signup031.storage import create_run_directory, write_evidence
from signup031.web_scenario import execute_scenario, load_scenario, validate_scenario


def run_scenario(config, artifacts_root):
    from playwright.sync_api import sync_playwright
    config = validate_scenario(config)
    execution_id, root = create_run_directory(Path(artifacts_root))
    started = utc_now()
    from signup031.timeline import Timeline
    timeline=Timeline(execution_id,'configured-playwright')
    write_evidence(root / 'scenario.json', config)
    archive = root / 'archive'
    archive.mkdir()
    checks, cleanup_errors = [], []
    status, message = 'preparation_failed', 'Browser preparation did not finish'
    screenshot = {'status': 'not_collected', 'path': 'page.png', 'reason': None}
    browser = context = page = None
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(service_workers='block', accept_downloads=False,
                                              record_har_path=archive / 'resources.har',
                                              record_har_content='embed', record_har_mode='full')
                page = context.new_page()
                timeline.attach_playwright(context,page)
                checks = execute_scenario(page, config)
                status = 'passed' if all(c['passed'] for c in checks) else 'failed'
                message = f'{sum(c["passed"] for c in checks)}/{len(checks)} checks passed'
            except Exception as exc:
                message = f'{type(exc).__name__}: {str(exc)[:2000]}'
            finally:
                if page is not None:
                    timeline.observe_dom(page)
                    try:
                        page.screenshot(path=str(root / 'page.png'), full_page=True, timeout=5000)
                        screenshot['status'] = 'collected'
                    except Exception as exc:
                        screenshot.update(status='collection_failed', reason=str(exc)[:800])
                for resource in (context, browser):
                    if resource is not None:
                        try:
                            resource.close()
                        except Exception as exc:
                            cleanup_errors.append(str(exc)[:800])
    except Exception as exc:
        cleanup_errors.append(str(exc)[:800])
        if status == 'preparation_failed':
            message = f'{type(exc).__name__}: {str(exc)[:2000]}'
    recording = finish_scenario_archive(archive, execution_id=execution_id, config=config,
                                        checks=checks, completed=status != 'preparation_failed',
                                        close_errors=cleanup_errors)
    payload = {
        'contract_version': '2', 'tc_id': config['id'], 'scenario_snapshot': config,
        'execution': {'id': execution_id, 'started_at': started, 'finished_at': utc_now()},
        'target': {'kind': 'configured_web', 'url': config['url']},
        'result': {'business': {'status': status,
                               'phase': 'checks' if status != 'preparation_failed' else 'preparation',
                               'message': message},
                   'runner': {'name': 'web_scenario', 'exit_code': 0 if status == 'passed' else 1}},
        'checks': checks, 'evidence': {'screenshot': screenshot}, 'replay': recording,
        'post_run': {'cleanup': {'status': 'failed' if cleanup_errors else 'completed',
                                 'errors': cleanup_errors}},
    }
    evidence_path = root / 'evidence.json'
    from signup031.timeline import save_optional
    save_optional(payload,timeline,root)
    write_evidence(evidence_path, payload)
    return evidence_path


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run an explicitly configured web TC and record its resources')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--config', type=Path)
    source.add_argument('--stdin-config', action='store_true')
    parser.add_argument('--artifacts-dir', type=Path, default=Path('artifacts/web-scenarios'))
    args = parser.parse_args(argv)
    try:
        config = load_scenario(args.config) if args.config else validate_scenario(json.load(sys.stdin))
        path = run_scenario(config, args.artifacts_dir)
        payload = json.loads(path.read_text(encoding='utf-8'))
        print(json.dumps({'evidence': str(path), 'status': payload['result']['business']['status']}, ensure_ascii=True), flush=True)
        return payload['result']['runner']['exit_code']
    except Exception as exc:
        print(json.dumps({'status': 'error', 'reason': str(exc)[:2000]}, ensure_ascii=True), flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
