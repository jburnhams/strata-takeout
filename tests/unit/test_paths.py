import pytest
from strata_takeout.paths import NAME_LIMIT, PATH_LIMIT, blen, safe_parts, shorten

def test_shorten_is_deterministic_unique_and_within_target():
    a, b = 'x' * 300 + 'A.txt', 'x' * 300 + 'B.txt'
    sa, sb = shorten(a), shorten(b)
    assert sa == shorten(a) and sa != sb and blen(sa) <= 200 and sa.endswith('.txt') and sa.startswith('xxxx')       # same name -> same result, different long names never collide

def test_shorten_cuts_on_a_character_boundary():
    s = shorten('é' * 200 + '.json')                                       # 400 bytes of 2-byte characters
    assert blen(s) <= 200 and s.endswith('.json') and 'é' in s and '�' not in s

def test_safe_parts_drops_leading_dir_and_nested_exports():
    assert safe_parts('Takeout/Drive/a/b.txt', '/srv/gt') == ['Drive', 'a', 'b.txt']
    assert safe_parts('Takeout/Drive/Takeout/takeout-old.zip', '/srv/gt') is None and safe_parts('Takeout/Drive/Takeout/sub/deep.zip', '/srv/gt') is None
    assert safe_parts('Takeout/Drive/Takeout2/ok.txt', '/srv/gt') == ['Drive', 'Takeout2', 'ok.txt']             # only the exact directory is nested
    assert safe_parts('Takeout/Drive/docs/Takeout.txt', '/srv/gt') == ['Drive', 'docs', 'Takeout.txt']

def test_safe_parts_never_escapes():
    assert safe_parts('Takeout/../../evil.txt', '/srv/gt') == ['evil.txt']
    assert safe_parts('/abs/path/x', '/srv/gt') == ['path', 'x']

def test_safe_parts_shortens_long_components_and_deep_paths():
    long = safe_parts('Takeout/Drive/' + 'q' * 300 + '.txt', '/srv/gt')
    assert blen(long[-1]) <= NAME_LIMIT
    deep = safe_parts('Takeout/' + '/'.join(['s' * 200] * 25) + '/deep.txt', '/srv/gt')
    assert blen('/srv/gt') + 1 + sum(blen(p) + 1 for p in deep) <= PATH_LIMIT

def test_safe_parts_gives_up_on_impossible_paths():
    with pytest.raises(OSError): safe_parts('Takeout/' + '/'.join(['d'] * 3000), '/srv/gt')                   # thousands of 1-byte directories cannot be shortened

def test_strip_and_nested_are_configurable():
    assert safe_parts('Takeout/Drive/a.txt', '/d', strip=0) == ['Takeout', 'Drive', 'a.txt']
    assert safe_parts('export/Drive/Takeout/x.zip', '/d', nested=(), strip=1) == ['Drive', 'Takeout', 'x.zip']              # nothing is skipped when nested is empty
    assert safe_parts('export/Backups/old/x.zip', '/d', nested=('Backups', 'old'), strip=1) is None
