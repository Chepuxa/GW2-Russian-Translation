"""Check the packaged translator's actual wire protocol in an installed fixture."""
from pathlib import Path
import json
import queue
import struct
import subprocess
import sys
import threading

import xxhash


def main():
    module=Path(sys.argv[1]).resolve()
    command=[str(module / "Local_Translator_RU.exe"),"--lang","ru","--no-update","--editor-port","0","--cache-db",str(module / "protocol-smoke.db")]
    if "--cached-only" in sys.argv:
        command.append("--fake")  # Cache misses would produce a detectable [RU] fallback.
    process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
    messages=queue.Queue()

    def read():
        try:
            while header:=process.stdout.read(4):
                size,=struct.unpack("<I",header)
                messages.put(process.stdout.read(size))
        except Exception as exc:
            messages.put(exc)

    threading.Thread(target=read,daemon=True).start()
    cases={567188:"Corruption Training",1000000001:"Inventory",1000000002:"Speak with the scout.",1000000003:"You have %num1% coins.<br><c=@abilitytype>Speak with the scout.</c>",1000000004:"Welcome to Guild Wars 2."}
    try:
        hello=messages.get(timeout=20)
        protocol,source_language,version=struct.unpack_from("<IiI",hello)
        key_length,=struct.unpack_from("<H",hello,12)
        cache_key=hello[14:14+key_length].decode("utf-8")
        assert (protocol,source_language,cache_key)==(2,0,"ru")
        hashes={}
        for key,en in cases.items():
            encoded=en.encode("utf-8")
            hashes[key]=xxhash.xxh3_64_intdigest(encoded)
            packet=struct.pack("<BIQH",0,key,hashes[key],len(encoded))+encoded
            process.stdin.write(struct.pack("<I",len(packet))+packet)
        process.stdin.flush()
        replies={}
        while len(replies)<len(cases):
            message=messages.get(timeout=45)
            assert isinstance(message,bytes),repr(message)
            if not message or message[0]!=0:
                continue
            key,h,length=struct.unpack_from("<IQH",message,1)
            if key not in cases:
                continue  # Startup can push the reviewed catalog before replies.
            assert h==hashes[key]
            replies[key]=message[15:15+length].decode("utf-8")
        assert replies[567188]=="Обучение порче"
        assert replies[1000000001]=="Инвентарь"
        assert "разведчик" in replies[1000000002].lower()
        for token in ("%num1%","<br>","<c=@abilitytype>","</c>"):
            assert token in replies[1000000003]
        assert "Guild Wars 2" in replies[1000000004]
        report={"passed":True,"protocol":protocol,"cache_key":cache_key,"result_version":version,"cached_only":"--cached-only" in sys.argv,"translations":replies}
        (module / "protocol-smoke-result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(report,ensure_ascii=False,indent=2))
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__=="__main__":
    main()
