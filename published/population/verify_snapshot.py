"""Check the recorded population release locally, without accessing the live site."""
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def read(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def check():
    manifest = read("SHA256SUMS.json")
    for name, expected in manifest.items():
        path = ROOT / name
        assert path.resolve().is_relative_to(ROOT), name
        assert path.is_file() and not path.is_symlink(), name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, name

    release = read("publication.json")
    posts = release["published_posts"]
    assert len(posts) == 50
    assert len({p["id"] for p in posts}) == 50
    assert len({p["url"] for p in posts}) == 50
    assert len(list((ROOT / "site").rglob("*.*"))) == 178
    assert len(list((ROOT / "articles").rglob("*.txt"))) == 60
    assert len(list((ROOT / "wordpress").rglob("*.html"))) == 50
    assert all(p in manifest for p in (
        str(f.relative_to(ROOT)) for folder in ("site", "articles", "wordpress")
        for f in (ROOT / folder).rglob("*") if f.is_file()
    ))

    saved = {p["id"]: p for p in read("verification/saved-wordpress.json")}
    public = {p["id"]: p for p in read("verification/public-articles.json")}
    for post in posts:
        body = ROOT / post["body_file"]
        assert hashlib.sha256(body.read_bytes()).hexdigest() == post["body_sha256"]
        assert post["body_sha256"] == saved[post["id"]]["sha256"]
        assert all(saved[post["id"]][k] for k in (
            "body_matches", "title_matches", "slug_matches"
        ))
        assert saved[post["id"]]["status"] == post["status"] == "publish"
        assert public[post["id"]]["matches"]
        assert (ROOT / post["source_article"]).is_file()
        assert len(post["embeds"]) == (1 if post["slug"].startswith("population-national") else 2)
        for url in post["embeds"]:
            path = url.removeprefix("https://app-navi.biz/").split("?", 1)[0]
            assert "site/" + path in manifest, path
        assert body.read_text(encoding="utf-8").count("[advanced_iframe ") == 1

    assets = read("verification/live-assets.json")
    assert len(assets) == release["deployed_asset_count"] == 164
    for asset in assets:
        assert asset["matches"]
        assert manifest["site/" + asset["path"]] == asset["actual_sha256"]
    cards = read("verification/card-resources.json")
    assert len(cards) == 45
    for card in cards:
        assert card["status"] == 200 and card["matches"]
        assert manifest["site/" + card["path"]] == card["actual_sha256"]
    assert len(release["shared_dependencies"]) == release["existing_dependency_count"] == 14
    for dependency in release["shared_dependencies"]:
        assert manifest["site/" + dependency["path"]] == dependency["sha256"]
    unrelated = read("verification/unrelated-posts.json")
    for key in ("new_ids", "removed_ids", "other_content_changes", "target_taxonomy_changes"):
        assert not unrelated[key], key
    assert not read("verification/translation-structure.json")["errors"]
    print(f"Verified {len(manifest)} files, 50 published posts, 164 deployed assets and 14 dependencies.")
    print("This validates the recorded release; it does not perform a fresh live-site or native-language audit.")


if __name__ == "__main__":
    check()
