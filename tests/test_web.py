from __future__ import annotations

import io

import pytest

from smart_ledger import create_app


@pytest.fixture
def client(test_config):
    app = create_app(test_config)
    app.config["TESTING"] = True
    return app.test_client()


def test_web_flow(client, fixture_csv_bytes):
    assert client.get("/").status_code == 200
    assert client.get("/transactions").status_code == 200
    assert client.get("/review").status_code == 200
    assert client.get("/rules").status_code == 200
    assert client.get("/import").status_code == 200

    # プレビュー(保存されない)
    r = client.post("/import/preview", data={"csv_file": (io.BytesIO(fixture_csv_bytes), "test.csv")}, content_type="multipart/form-data")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "10 件を取り込む" in html
    assert "テストデンリヨク 8ガツブン" in html
    token = html.split('name="token" value="')[1].split('"')[0]
    assert client.get("/transactions").get_data(as_text=True).count("編集") == 0

    # 取込確定(APIキー無し → 全件要確認)
    r = client.post("/import/commit", data={"token": token}, follow_redirects=True)
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "10 件を取り込みました" in body

    # ダッシュボード: 8月 総支出 = 7850+3240+10000+5000+5000+10000+4500 = 45590
    dash = client.get("/?month=2026-08").get_data(as_text=True)
    assert "45,590" in dash

    # 明細編集: 今後この加盟店も → ルール登録
    # SAMPLE WALLET の明細を編集対象にする
    tx_html = client.get("/transactions?q=SAMPLE").get_data(as_text=True)
    tx_id = tx_html.split("/transactions/")[1].split("/edit")[0]
    r = client.post(f"/transactions/{tx_id}/category", data={"category": "その他", "scope": "always", "memo": "wallet"}, follow_redirects=True)
    assert "ルールを登録しました" in r.get_data(as_text=True)
    assert "SAMPLE WALLET" in client.get("/rules").get_data(as_text=True)

    # 内訳: 合計不一致はエラー、一致すれば保存
    r = client.post(
        f"/transactions/{tx_id}/allocations",
        data={"alloc_category": ["食費", "外食"], "alloc_amount": ["6000", "3000"], "alloc_memo": ["", ""]},
        follow_redirects=True,
    )
    assert "一致しません" in r.get_data(as_text=True)
    r = client.post(
        f"/transactions/{tx_id}/allocations",
        data={"alloc_category": ["食費", "外食"], "alloc_amount": ["6000", "4000"], "alloc_memo": ["a", "b"]},
        follow_redirects=True,
    )
    assert "内訳を保存しました" in r.get_data(as_text=True)
    dash = client.get("/?month=2026-08").get_data(as_text=True)
    assert "45,590" in dash  # 総支出は変わらない(二重計上なし)

    # 再取込は拒否
    r = client.post("/import/preview", data={"csv_file": (io.BytesIO(fixture_csv_bytes), "test.csv")}, content_type="multipart/form-data")
    assert "このCSVはすでに取り込み済みです" in r.get_data(as_text=True)

    # 要確認から確定
    review = client.get("/review").get_data(as_text=True)
    assert "要確認" in review
