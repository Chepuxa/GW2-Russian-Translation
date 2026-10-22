"""Exercise the real packaged module and PS 5.1 installer in isolated fixtures."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tomllib
import uuid
import zipfile

ROOT=Path(__file__).resolve().parents[1]
PACKAGE=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else ROOT / "dist/GW2-Russian-Translation"
FIXTURES=ROOT / ".artifacts" / ("install-smoke-"+uuid.uuid4().hex[:8])


def game(name):
    path=FIXTURES / name
    path.mkdir(parents=True)
    (path / "Gw2-64.exe").write_bytes(b"fixture; never executed")
    (path / "Gw2.dat").write_bytes(b"fixture; never scanned")
    return path


def run(script,target,*arguments,success=True):
    result=subprocess.run(["powershell.exe","-NoProfile","-ExecutionPolicy","Bypass","-File",str(PACKAGE / "installer" / script),"-GamePath",str(target),*map(str,arguments)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    if (result.returncode==0)!=success:
        raise AssertionError((script,result.returncode,result.stdout.decode("utf-8","replace")))
    return result


def module(game):
    return game / "addons/text_translator/modules/local_translator_ru"


def settings(game):
    return tomllib.loads((game / "addons/text_translator/settings.toml").read_text(encoding="utf-8-sig"))


def main():
    first=game("Другой $ ПК GW2")
    run("Install-Translator.ps1",first)
    assert bytes(settings(first)["active_module"]["Windows"]).decode()=="local_translator_ru"
    glyph=(first / "glyphCore/config.ini").read_text(encoding="utf-8")
    assert "glyph_size=90" in glyph and "glyph_baseline=80" in glyph
    assert "translate=false" in glyph and "upload=false" in glyph
    assert str(first / "glyphCore/cyrillic.ttf") in glyph
    expected=json.loads((PACKAGE / "payload/cache-manifest.json").read_text(encoding="utf-8"))
    with (module(first) / "RU/cache_ru.jsonl").open(encoding="utf-8") as stream:
        assert sum(1 for _ in stream)==expected["cached_lines"]
    with closing(sqlite3.connect(module(first) / "cache.db")) as db:
        assert db.execute("SELECT COUNT(*) FROM translations").fetchone()[0]==expected["game_cache_entries"]
    assert (PACKAGE / "data/imported/corpus-english.csv").is_file()
    assert len(list((PACKAGE / "drafts/ru").glob("p-*.json")))==256
    assert (module(first) / "community/known-strings.db").is_file()
    assert (module(first) / "tools/github/gh.exe").is_file()
    assert (PACKAGE / "Contribute.cmd").is_file()
    queue_directory=module(first) / "RU/contributions"
    queue_directory.mkdir(parents=True)
    with closing(sqlite3.connect(queue_directory / "inbox.db")) as db,db:
        db.execute("CREATE TABLE personal_marker(value TEXT)")
        db.execute("INSERT INTO personal_marker VALUES('preserve local queue')")
    with closing(sqlite3.connect(module(first) / "RU/patch_ru.db")) as db,db:
        assert db.execute("SELECT COUNT(*) FROM patch WHERE kind=0 AND src_hash IS NOT NULL").fetchone()[0]==232
        assert db.execute("SELECT text FROM patch WHERE key=567188").fetchone()[0]=="Обучение порче"
        db.execute("INSERT INTO patch VALUES(900002,0,'User reviewed text',123,NULL,1)")
    with closing(sqlite3.connect(module(first) / "cache.db")) as db,db:
        db.execute("INSERT INTO translations VALUES(3500000001,'ru',123,1,123,'User cached text')")
    (first / "addons/unrelated.bin").write_bytes(b"keep me")
    path=first / "addons/text_translator/settings.toml"
    path.write_text(path.read_text(encoding="utf-8").replace("pause_refreshes = true","pause_refreshes = false"),encoding="utf-8")
    run("Install-Translator.ps1",first)
    assert settings(first)["pause_refreshes"] is False
    with closing(sqlite3.connect(queue_directory / "inbox.db")) as db:
        assert db.execute("SELECT value FROM personal_marker").fetchone()[0]=='preserve local queue'
    assert (first / "addons/unrelated.bin").read_bytes()==b"keep me"
    with closing(sqlite3.connect(module(first) / "cache.db")) as db:
        assert db.execute("SELECT text FROM translations WHERE id=3500000001").fetchone()[0]=="User cached text"
    with closing(sqlite3.connect(module(first) / "RU/patch_ru.db")) as db:
        assert db.execute("SELECT text FROM patch WHERE key=900002").fetchone()[0]=="User reviewed text"
    run("Disable-Translator.ps1",first)
    assert settings(first)["show_translated"] is False
    run("Enable-Translator.ps1",first)
    assert settings(first)["show_translated"] is True
    archive=FIXTURES / "personal-cache.zip"
    run("Export-Cache.ps1",first,"-OutputPath",archive)
    with zipfile.ZipFile(archive) as bundle:
        assert "RU/patch_ru.db" in bundle.namelist()
        assert not any("contributions" in name for name in bundle.namelist())
    second=game("Second PC")
    run("Install-Translator.ps1",second)
    run("Import-Cache.ps1",second,"-CachePath",archive)
    with closing(sqlite3.connect(module(second) / "cache.db")) as db:
        assert db.execute("SELECT text FROM translations WHERE id=3500000001").fetchone()[0]=="User cached text"
    with closing(sqlite3.connect(module(second) / "RU/patch_ru.db")) as db:
        assert db.execute("SELECT text FROM patch WHERE key=900002").fetchone()[0]=="User reviewed text"
    run("Import-Cache.ps1",second,"-CachePath",archive)
    result=json.loads((module(second) / "precache/import-result.json").read_text(encoding="utf-8"))
    assert result["game_rows_added"]==0 and result["reviewed_rows_added"]==0
    conflict=game("Existing loader")
    (conflict / "d3d11.dll").write_bytes(b"unrelated loader")
    run("Install-Translator.ps1",conflict,success=False)
    assert (conflict / "d3d11.dll").read_bytes()==b"unrelated loader"
    tampered=game("Tampered package")
    payload=PACKAGE / "payload/profile.json"
    original=payload.read_bytes()
    try:
        payload.write_bytes(original+b" ")
        run("Install-Translator.ps1",tampered,success=False)
        assert not (tampered / "d3d11.dll").exists()
    finally:
        payload.write_bytes(original)
    report={"phase":"complete","reviewed":232,"cache":expected,"fixture_root":str(FIXTURES),"checks":["clean install without Python","whole bundled machine cache installed","editable full corpus and drafts included","Cyrillic and dollar-sign path","reinstall preserves cache and reviewed edits","preferences and unrelated addons preserved","font 90/80","enable/disable","personal cache and reviewed export/import","repeated import","loader conflict refused","tamper refused before writes"]}
    (FIXTURES / "result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
