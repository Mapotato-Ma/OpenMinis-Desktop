"""cc-switch 导入读取器的测试：只用手造库，不依赖机器上真装了 cc-switch。

盯三件事：① 只读（绝不改用户文件）② 抠不出值时给的是人话 ③ 掩码不泄密。
"""
import json
import sqlite3

from desktop import ccswitch_import as ci


def _mk_db(home, rows):
    d = home / '.cc-switch'
    d.mkdir(parents=True, exist_ok=True)
    db = d / 'cc-switch.db'
    con = sqlite3.connect(db)
    con.execute(
        'CREATE TABLE providers (id INTEGER PRIMARY KEY, app_type TEXT, name TEXT,'
        ' settings_config TEXT, is_current INTEGER)'
    )
    con.executemany(
        'INSERT INTO providers (app_type, name, settings_config, is_current) VALUES (?,?,?,?)',
        rows,
    )
    con.commit()
    con.close()
    return db


def test_scan_maps_types_and_masks_keys(tmp_path):
    _mk_db(tmp_path, [
        ('claude', 'PackyAPI', json.dumps({'env': {
            'ANTHROPIC_BASE_URL': 'https://api.packy.dev',
            'ANTHROPIC_AUTH_TOKEN': 'sk-ant-abcdefghijklmnopqrst'}}), 1),
        ('gemini', 'Ambiguous', json.dumps({'env': {
            'GEMINI_API_KEY': 'AIzaSy1234567890',
            'GOOGLE_GEMINI_BASE_URL': 'https://a', 'GEMINI_BASE_URL': 'https://b'}}), 0),
    ])
    res = ci.scan(base=tmp_path)
    assert res['available'] is True
    by_name = {i['name']: i for i in res['items']}
    claude = by_name['PackyAPI']
    assert claude['suggestedType'] == 'anthropic'
    assert claude['baseUrl'] == 'https://api.packy.dev'
    assert claude['mask'] == 'sk-\u2026qrst'
    # 关键：整个响应里不许出现明文
    assert 'abcdefghijklmnopqrst' not in json.dumps(res, ensure_ascii=False)
    amb = by_name['Ambiguous']
    assert amb['baseUrl'] == '' and any('手填' in n for n in amb['notes'])


def test_scan_never_writes_to_the_user_store(tmp_path):
    db = _mk_db(tmp_path, [('claude', 'A', json.dumps({'env': {'ANTHROPIC_BASE_URL': 'https://a'}}), 0)])
    before = (db.read_bytes(), db.stat().st_mtime_ns, sorted(p.name for p in db.parent.iterdir()))
    ci.scan(base=tmp_path)
    ci.entries([1], base=tmp_path)
    after = (db.read_bytes(), db.stat().st_mtime_ns, sorted(p.name for p in db.parent.iterdir()))
    assert before == after, '读取过程中动了用户的 cc-switch 目录'


def test_missing_store_says_something_human(tmp_path):
    res = ci.scan(base=tmp_path / 'nope')
    assert res['available'] is False
    assert res['items'] == [] and res['reason']


def test_oauth_only_row_is_flagged_not_imported(tmp_path):
    _mk_db(tmp_path, [
        ('codex', 'OAuth', json.dumps({'auth': {'tokens': {'refresh_token': 'r-123'}}}), 0),
        ('grok', 'Grok 家', json.dumps({'env': {'XAI_API_KEY': 'xai-1234567890abcdef'}}), 1),
    ])
    res = ci.scan(base=tmp_path)
    got = {i['name']: i for i in res['items']}
    oauth = got['OAuth']
    assert oauth['hasKey'] is False and oauth['mask'] == ''
    assert any('OAuth' in n for n in oauth['notes'])
    grok = got['Grok 家']
    assert grok['suggestedType'] == 'xAI' and grok['hasKey'] is True


def test_entries_only_returns_requested_rows_with_plaintext(tmp_path):
    _mk_db(tmp_path, [
        ('claude', 'A', json.dumps({'env': {'ANTHROPIC_BASE_URL': 'https://a', 'ANTHROPIC_AUTH_TOKEN': 'sk-1234567890abcdef'}}), 0),
    ])
    # id 一律按字符串传（真实库里是 UUID）
    out = ci.entries(['1', '7'], base=tmp_path)
    assert len(out['items']) == 1 and out['missing'] == ['7']
    assert out['items'][0]['apiKey'] == 'sk-1234567890abcdef'


def test_mask_key_short_secret_is_fully_hidden():
    assert ci.mask_key('sk-123') == '\u2022' * 8      # 短 key 露头露尾就等于全泄
    assert ci.mask_key('') == ''
    assert ci.mask_key('sk-abcdefghij') == 'sk-\u2026ghij'


def test_codex_toml_config_is_parsed(tmp_path):
    toml = 'model_provider = "x"\nmodel = "gpt-5-codex"\nbase_url = "https://codex.example/v1"\n'
    _mk_db(tmp_path, [
        ('codex', 'Codex 家', json.dumps({'auth': {'OPENAI_API_KEY': 'sk-openai-1234567890'}, 'config': toml}), 1),
    ])
    item = ci.scan(base=tmp_path)['items'][0]
    assert item['suggestedType'] == 'openAI'
    assert item['baseUrl'] == 'https://codex.example/v1'
    assert item['model'] == 'gpt-5-codex'


def test_real_world_shape_uuid_ids_and_many_default_models(tmp_path):
    """按真实库的样本钉住：id 是 TEXT，claude 行里有 ANTHROPIC_MODEL + 一堆 DEFAULT_*_MODEL。"""
    d = tmp_path / '.cc-switch'
    d.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(d / 'cc-switch.db')
    con.execute('CREATE TABLE providers (id TEXT PRIMARY KEY, app_type TEXT, name TEXT,'
                ' settings_config TEXT, is_current BOOLEAN, provider_type TEXT)')
    cfg = json.dumps({'env': {
        'ANTHROPIC_BASE_URL': 'http://192.168.20.200:3000',
        'ANTHROPIC_AUTH_TOKEN': 'sk-real-1234567890abcdef',
        'ANTHROPIC_MODEL': 'deepseek-v4-pro',
        'ANTHROPIC_DEFAULT_HAIKU_MODEL': 'deepseek-v4-flash',
        'ANTHROPIC_DEFAULT_SONNET_MODEL': 'deepseek-v4-pro',
    }})
    con.executemany('INSERT INTO providers VALUES (?,?,?,?,?,?)', [
        ('产研公共token-1785201979407', 'claude', '产研公共token', cfg, 1, 'custom'),
        ('gemini-official', 'gemini', 'Google Official', '{}', 0, 'official'),
    ])
    con.commit()
    con.close()
    res = ci.scan(base=tmp_path)
    got = {i['name']: i for i in res['items']}
    assert got['产研公共token']['suggestedType'] == 'anthropic'
    # 关键：多个 MODEL 值不同时，要精确取 ANTHROPIC_MODEL，而不是判成歧义留空
    assert got['产研公共token']['model'] == 'deepseek-v4-pro'
    assert got['产研公共token']['mask'] == 'sk-\u2026cdef'
    official = got['Google Official']
    assert official['baseUrl'] == '' and official['hasKey'] is False
    # UUID 这类字符串 id 必须能原样取回
    out = ci.entries(['产研公共token-1785201979407'], base=tmp_path)
    assert len(out['items']) == 1 and out['missing'] == []
    assert out['items'][0]['apiKey'] == 'sk-real-1234567890abcdef'
