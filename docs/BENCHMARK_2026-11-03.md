# Erste Messreihe mit kleinen Modellen (nur CPU)

**Datum:** 2026-11-03, ca. 03:00 Uhr. **Werkzeug:** `scripts/bench_ollama.py` (Stream-Messung über Loopback). **Host:** Intel Core i9-9900K, 15 GiB RAM, Ollama 0.32.14. **GPU:** nicht verwendet (`num_gpu=0`); GPU-Messung braucht eine eigene Freigabe.

Feste synthetische Prompts (Arithmetik, kurze Python-Funktion, Ein-Satz-Zusammenfassung), `num_ctx=1024`, `num_predict=64`, `num_batch=32`, `temperature=0`, `seed=42`. Antworttexte wurden nicht gespeichert. Rohdaten liegen nur lokal unter `.pytest_cache/bench/` (nicht versioniert).

## Ergebnisse

| Modell | Quant | Threads | Kalt-Load s | TTFT p50 / p95 s | Ausgabe tok/s p50 | Prompt tok/s p50 | Wall p50 / p95 s | CPU-Temp vor / max °C | n |
|---|---|---|---|---|---|---|---|---|---|
| qwen2.5-coder:1.5b | Q4_K_M | 2 | 3,22 | 0,43 / 0,96 | 23,7 | 145,5 | 1,18 / 1,95 | 42 / 55 | 10 |
| qwen2.5-coder:1.5b | Q4_K_M | 4 | 1,45¹ | 0,29 / 0,56 | 32,7 | 282,3 | 0,90 / 1,28 | 49 / 58 | 10 |
| qwen2.5-coder:7b-instruct | Q4_K_M | 4 | 10,75 | 1,05 / 2,18 | 7,7 | 52,2 | 3,66 / 4,97 | 47 / 56 | 5 |

¹ Der zweite 1.5B-Lauf folgte direkt auf den ersten; die Gewichte lagen vermutlich noch im Page-Cache. Der Kaltstart ist daher nicht vergleichbar.

Alle drei Läufe: Arithmetik-Prüfung **PASS**, danach **0** geladene Modelle. Ausgabelängen 2–23 Tokens pro Lauf; die Raten beruhen also auf kurzen Antworten.

## GPU-Messung (Freigabe G3 vom 2026-11-03)

Gleiche Prompts und Optionen wie oben, zusätzlich `num_gpu=99` (alle Schichten auf die GTX 1660, 6 GiB). Gemessen ca. 10:16 Uhr.

| Modell | Threads | Kalt-Load s | TTFT p50 / p95 s | Ausgabe tok/s p50 | Prompt tok/s p50 | Wall p50 / p95 s | VRAM MiB | GPU-Leistung max W | GPU-Temp vor / max °C | n |
|---|---|---|---|---|---|---|---|---|---|---|
| qwen2.5-coder:1.5b | 4 | 4,17 | 0,18 / 0,24 | 126,1 | 1014,7 | 0,32 / 0,43 | ~1590 | 105 | 28 / 36 | 10 |
| qwen2.5-coder:7b-instruct | 4 | 9,97 | 0,31 / 0,57 | 35,6 | 271,9 | 0,87 / 1,16 | ~4850 | 116 | 31 / 39 | 5 |

Gegenüber CPU mit 4 Threads: 1.5B etwa 3,9× und 7B etwa 4,6× schnellere Ausgabe (Median). Beide Läufe: Arithmetik-Prüfung **PASS**, danach **0** geladene Modelle. Läufe mit nur 2 Ausgabe-Tokens zeigen unplausibel hohe Raten (bis 241 tok/s); der Median stammt aus den Läufen mit 20–23 Tokens. GPU-Werte sind Momentaufnahmen nach jedem Lauf (`nvidia-smi`), keine Energiemessung.

## Zweites Gerät: Fedora-Laptop (nur CPU)

Gemessen am 2026-11-03 gegen 12:20 UTC von der Claude-Session auf dem Laptop selbst und per Nachricht übermittelt; die Rohdaten liegen nur auf dem Laptop. Intel Core i5 10. Generation (8 logische CPUs), 8 GB RAM (7,4 GiB nutzbar), keine dedizierte GPU. Gleiche Prompts und Optionen wie oben, `num_gpu=0`, Worker während der Messung gestoppt.

| Modell | Threads | Kalt-Load s | TTFT p50 / p95 s | Ausgabe tok/s p50 | Prompt tok/s p50 | Wall p50 / p95 s | n |
|---|---|---|---|---|---|---|---|
| qwen2.5-coder:1.5b | 2 | 2,60 | 0,59 / 1,43 | 12,9 | 73,7 | 1,87 / 3,24 | 10 |
| qwen2.5-coder:1.5b | 4 | 2,34 | 0,32 / 0,85 | 17,7 | 135,7 | 1,36 / 2,16 | 10 |

Arithmetik-Prüfung beide Male **PASS**. Das sind ca. 54 % der Ausgaberate des Desktops (i9-9900K) bei gleicher Thread-Zahl. RAM im Leerlauf 4,2 GB verfügbar, unter Last 3,6 GB; CPU-Package-Temperatur 43 °C im Leerlauf, max. 52 °C unter Last.

Im Grid (Laptop als Worker über den SSH-Tunnel, Job vom Desktop-Koordinator): zwei echte Jobs **completed** in 6,5 s und 6,0 s vom Absenden bis zum Ergebnis (inklusive Abfrageintervall des Workers von bis zu 3 s und Laden des Modells). Der zweite Job lief bereits unter dem systemd-User-Dienst.



`scripts/smoke_test_ollama.py --model qwen2.5-coder:1.5b --min-free-gib 3`: **PASS**. Echter Koordinator → Worker → Ollama → Ergebnis, Worker-Prozess 2,744 s inklusive Laden, Nutzer 100 → 95 Credits, Worker 5 provisorisch / 0 ausgebbar, Antwortprüfung PASS, danach keine Modelle geladen. Zum Vergleich: derselbe Test mit 7B dauerte am 2026-11-02 16,818 s (2 Threads).

Mit GPU (`smoke_test_ollama.py --num-gpu 99 --i-approve-gpu`, neu): 1.5B **PASS** in 4,998 s, 7B **PASS** in 4,736 s, jeweils inklusive Laden, gleiche Credit-Prüfungen, danach keine Modelle geladen. Bei einem einzigen kurzen Job dominiert das Laden in den GPU-Speicher; deshalb ist 1.5B hier langsamer als auf der CPU. Die 7B-Gewichte lagen vermutlich noch aus der Messung im Page-Cache.

## Grenzen

- Ein Rechner, kleine Stichprobe (n=5–10), feste kurze Prompts. Das ist kein Durchsatz- oder Qualitätsbenchmark. p95 bei n=10 ist der zweitgrößte Wert.
- Keine Energiemessung. Die CPU-Temperatur ist eine Momentaufnahme pro Lauf (`x86_pkg_temp`). In der CPU-Reihe wurden GPU-Leistung und -Temperatur nur im Leerlauf erfasst (ca. 13 W / 31 °C).
- Nicht gemessen: teilweiser GPU-Offload, Parallelität mehrerer Worker mit echtem Modell, längere Kontexte/Antworten.
- Beobachtung aus dem Nachtlauf: `qwen2.5-coder:7b-instruct` mit 16k Kontext und teilweisem GPU-Offload belegte nach einigen Stunden ca. 8,7 GB RAM. Zusammen mit dem Desktop war der Swap voll (4/4 GiB), und die Generierung fiel von ~8 auf ~0,5 tok/s. Der Lauf wurde deshalb abgebrochen und das Modell entladen.
