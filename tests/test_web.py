"""Flask test client による画面フローのテスト"""

from __future__ import annotations

import io

import pytest
from flask.testing import FlaskClient

from smart_ledger import create_app
from smart_ledger.config import Config


@pytest.fixture
def client(test_config: Config) -> FlaskClient:
    """テスト用アプリのクライアント

    Args:
        test_config: 一時ディレクトリを使う設定
    """
    app = create_app(test_config)
    app.config['TESTING'] = True
    return app.test_client()


def upload(client: FlaskClient, content: bytes, filename: str = 'test.csv') -> str:
    """CSV をアップロードしてプレビュー HTML を返す

    Args:
        client: テストクライアント
        content: CSV のバイト列
        filename: ファイル名
    """
    response = client.post(
        '/import/preview', data={'csv_file': (io.BytesIO(content), filename)}, content_type='multipart/form-data'
    )
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_pages_render(client: FlaskClient) -> None:
    """データが無い状態でも各画面が 200 を返す

    Args:
        client: テストクライアント
    """
    for path in ('/', '/transactions', '/review', '/rules', '/categories', '/import', '/health'):
        assert client.get(path).status_code == 200


def test_web_flow(client: FlaskClient, fixture_csv_bytes: bytes) -> None:
    """取込 → ダッシュボード → カテゴリ変更（ルール登録） → 内訳 → 再取込拒否 の一連のフロー

    Args:
        client: テストクライアント
        fixture_csv_bytes: CP932 の fixture
    """
    html = upload(client, fixture_csv_bytes)
    assert '10 件を取り込む' in html
    assert 'テストデンリヨク 8ガツブン' in html
    token = html.split('name="token" value="')[1].split('"')[0]
    assert client.get('/transactions').get_data(as_text=True).count('編集') == 0  # プレビューでは保存されない

    body = client.post('/import/commit', data={'token': token}, follow_redirects=True).get_data(as_text=True)
    assert '10 件を取り込みました' in body

    # 8月 総支出 = 7850+3240+10000+5000+5000+10000+4500 = 45590
    assert '45,590' in client.get('/?month=2026-08').get_data(as_text=True)

    tx_html = client.get('/transactions?q=SAMPLE').get_data(as_text=True)
    tx_id = tx_html.split('/transactions/')[1].split('/edit')[0]
    response = client.post(
        f'/transactions/{tx_id}/category',
        data={'category': 'その他', 'scope': 'always', 'memo': 'wallet'},
        follow_redirects=True,
    )
    assert 'ルールを登録しました' in response.get_data(as_text=True)
    assert 'SAMPLE WALLET' in client.get('/rules').get_data(as_text=True)

    response = client.post(
        f'/transactions/{tx_id}/allocations',
        data={'alloc_category': ['食費', '外食'], 'alloc_amount': ['6000', '3000'], 'alloc_memo': ['', '']},
        follow_redirects=True,
    )
    assert '一致しません' in response.get_data(as_text=True)

    response = client.post(
        f'/transactions/{tx_id}/allocations',
        data={'alloc_category': ['食費', '外食'], 'alloc_amount': ['6000', '4000'], 'alloc_memo': ['a', 'b']},
        follow_redirects=True,
    )
    assert '内訳を保存しました' in response.get_data(as_text=True)
    assert '45,590' in client.get('/?month=2026-08').get_data(as_text=True)  # 総支出は変わらない

    assert 'このCSVはすでに取り込み済みです' in upload(client, fixture_csv_bytes)
    assert '要確認' in client.get('/review').get_data(as_text=True)


def test_invalid_month_param_is_ignored(client: FlaskClient) -> None:
    """不正な month はエラーにならず無視される

    Args:
        client: テストクライアント
    """
    assert client.get('/?month=abcdefg').status_code == 200
    assert client.get('/transactions?month=2026-13').status_code == 200


def test_rule_add_rejects_unknown_category(client: FlaskClient) -> None:
    """カテゴリ一覧に無いカテゴリのルールは登録されない

    Args:
        client: テストクライアント
    """
    response = client.post('/rules/add', data={'merchant_pattern': 'X', 'category': 'typo'}, follow_redirects=True)
    html = response.get_data(as_text=True)
    assert '不明なカテゴリです' in html and '→ typo' not in html


def test_external_back_is_replaced(client: FlaskClient, fixture_csv_bytes: bytes) -> None:
    """back に外部 URL を渡しても戻り先はサイト内に限定される

    Args:
        client: テストクライアント
        fixture_csv_bytes: CP932 の fixture
    """
    token = upload(client, fixture_csv_bytes).split('name="token" value="')[1].split('"')[0]
    client.post('/import/commit', data={'token': token})
    tx_html = client.get('/transactions?q=SAMPLE').get_data(as_text=True)
    tx_id = tx_html.split('/transactions/')[1].split('/edit')[0]
    html = client.get(f'/transactions/{tx_id}/edit?back=https://evil.example').get_data(as_text=True)
    assert 'evil.example' not in html
    for back in ('//evil.example', '/\\evil.example/p', 'https://evil.example'):
        response = client.post(f'/transactions/{tx_id}/category', data={'category': 'その他', 'back': back})
        assert response.headers['Location'] == '/transactions'


def test_always_scope_applies_to_same_merchant(client: FlaskClient, fixture_csv_bytes: bytes) -> None:
    """「今後この加盟店も」は同じ加盟店の他の明細にも反映され、件数が表示される

    Args:
        client: テストクライアント
        fixture_csv_bytes: CP932 の fixture
    """
    token = upload(client, fixture_csv_bytes).split('name="token" value="')[1].split('"')[0]
    client.post('/import/commit', data={'token': token})
    tx_html = client.get('/transactions?q=ホケン').get_data(as_text=True)
    tx_id = tx_html.split('/transactions/')[1].split('/edit')[0]
    response = client.post(
        f'/transactions/{tx_id}/category', data={'category': '保険・税金', 'scope': 'always'}, follow_redirects=True
    )
    assert '同じ加盟店の 1 件にも適用しました' in response.get_data(as_text=True)
    response = client.post(
        f'/transactions/{tx_id}/allocations',
        data={'alloc_category': ['食費'], 'alloc_amount': ['abc'], 'alloc_memo': ['']},
        follow_redirects=True,
    )
    assert '内訳の金額が数値ではありません' in response.get_data(as_text=True)


def test_cross_site_post_is_rejected(client: FlaskClient) -> None:
    """Origin のホストが一致しない POST は 403、一致すれば受け付ける

    Args:
        client: テストクライアント
    """
    data = {'merchant_pattern': 'X', 'category': 'その他'}
    assert client.post('/rules/add', data=data, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.post('/rules/add', data=data, headers={'Referer': 'https://evil.example/p'}).status_code == 403
    assert client.post('/rules/add', data=data, headers={'Origin': 'http://localhost'}).status_code == 302


def test_category_management_flow(client: FlaskClient) -> None:
    """カテゴリの追加 → 名称変更（ルールへ伝播） → 並び替え → 削除ガード → 削除

    Args:
        client: テストクライアント
    """
    body = client.post(
        '/categories/add', data={'name': '　ペット ', 'description': 'フード・動物病院'}, follow_redirects=True
    ).get_data(as_text=True)
    assert 'カテゴリ「ペット」を追加しました' in body  # 正規化後の名前で表示される
    assert 'フード・動物病院' in body

    body = client.post('/categories/add', data={'name': 'ペット'}, follow_redirects=True).get_data(as_text=True)
    assert '既に存在します' in body

    # ルールで使ってから名称変更すると伝播する
    client.post('/rules/add', data={'merchant_pattern': 'PETSHOP', 'category': 'ペット'}, follow_redirects=True)
    body = client.post(
        '/categories/edit',
        data={'category': 'ペット', 'new_name': 'ペット用品', 'description': ''},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert '名称変更を明細・ルール・内訳の 1 件に反映しました' in body
    assert 'ペット用品' in client.get('/rules').get_data(as_text=True)

    body = client.post(
        '/categories/move', data={'category': 'ペット用品', 'direction': 'up'}, follow_redirects=True
    ).get_data(as_text=True)
    assert '並び順を変更しました' in body

    body = client.post(
        '/categories/move', data={'category': 'その他', 'direction': 'down'}, follow_redirects=True
    ).get_data(as_text=True)
    assert '既に端にあるため並び順は変わりません' in body

    body = client.post('/categories/delete', data={'category': 'ペット用品'}, follow_redirects=True).get_data(
        as_text=True
    )
    assert '使用中のため削除できません' in body

    body = client.post('/categories/delete', data={'category': 'その他'}, follow_redirects=True).get_data(as_text=True)
    assert '削除できません' in body

    client.post('/rules/delete', data={'merchant_pattern': 'PETSHOP'}, follow_redirects=True)
    body = client.post('/categories/delete', data={'category': 'ペット用品'}, follow_redirects=True).get_data(
        as_text=True
    )
    assert 'カテゴリ「ペット用品」を削除しました' in body
    assert 'ペット用品' not in client.get('/categories').get_data(as_text=True)
