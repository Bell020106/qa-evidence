"""Read-only compatibility adapter for existing SIGNUP-031 archive version 1."""
import re
from signup031.contract import build_synthetic_password


def validate_legacy_procedure(manifest):
    procedure = manifest.get('procedure')
    if not isinstance(procedure, dict) or procedure.get('name') != 'SIGNUP-031' or procedure.get('initial_length') != 128 or procedure.get('append') != 'Z':
        raise ValueError('unsupported input procedure')
    for key in ('selector', 'label_pattern'):
        if procedure.get(key) is not None and not isinstance(procedure[key], str):
            raise ValueError('invalid locator')
    observed = manifest.get('observed', {})
    if observed.get('initial_length') != 128 or type(observed.get('after_extra_length')) is not int:
        raise ValueError('missing observed lengths')


def restore_legacy(page, manifest):
    page.goto(manifest['navigation_url'], wait_until='domcontentloaded', timeout=30000)
    procedure = manifest['procedure']
    field = (page.locator(procedure['selector']) if procedure.get('selector') else
             page.get_by_label(re.compile(procedure['label_pattern'], re.I)).first)
    field.wait_for(state='visible', timeout=10000)
    field.fill(build_synthetic_password())
    if len(field.input_value()) != 128:
        raise ValueError('initial restored length does not match 128')
    field.press_sequentially('Z')
    actual = len(field.input_value())
    if actual != manifest['observed']['after_extra_length']:
        raise ValueError(f'restored length mismatch: observed {actual}')
