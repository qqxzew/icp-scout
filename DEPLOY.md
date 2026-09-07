# Nasazení icp-scout

Prototyp běží na stejném stroji jako jiný web, ale jako **samostatný stack**:
vlastní tunel, vlastní docker síť, žádný publikovaný port na hostiteli. Ty dva
sdílejí jenom docker daemon, takže restart nebo přestavba jednoho nemůže
shodit druhý.

```
Cloudflare edge (TLS) ──▶ cloudflared ──▶ Caddy :80 ──▶ app:8000 (uvicorn)
                                                            │
                                                            └── /app/data  (bind mount)
                                                                /app/web   (bind mount)
```

Server nemá vlastní veřejnou IPv4. Dovnitř nevede nic — cloudflared se sám
připojuje ven k Cloudflare, takže není co vystavovat a není co certifikovat.

## 1. Doména

Doména se v tomhle repozitáři nikde nejmenuje. Je to *public hostname* na
tunelu, a nastavuje se v dashboardu, ne v souboru:

1. Cloudflare ▸ **Add a domain** — zóna se přidá do účtu, u registrátora se
   přepíšou nameservery na ty, které Cloudflare vypíše.
2. Zero Trust ▸ Networks ▸ **Tunnels** ▸ *Create a tunnel* ▸ **Cloudflared**.
   Vlastní tunel, ne ten stávající: druhý tunel nic nestojí a drží oba weby
   oddělené.
3. *Install connector* vypíše token. Ten patří do `.env` jako
   `CLOUDFLARE_TUNNEL_TOKEN` (příkaz z dashboardu se nespouští — kontejner
   `cloudflared` v compose dělá přesně to samé).
4. Záložka **Public Hostname** ▸ *Add*:
   - Subdomain: prázdné (apex) — a druhý záznam pro `www`, chce-li se
   - Domain: nová doména
   - Type: **HTTP**, URL: **`caddy:80`**

   DNS záznam (CNAME na `<tunnel-id>.cfargotunnel.com`) si Cloudflare založí
   sám.

Změna domény později je editace v dashboardu. Na serveru se nesahá na nic.

## 2. Server

```bash
cd /opt/icp-scout
cp deploy/.env.example .env   # vyplnit token a OPENAI_API_KEY
docker compose up -d --build
```

Bind mounty píše proces uvnitř kontejneru pod uid 1000 (obraz záměrně neběží
jako root), takže na hostiteli:

```bash
chown -R 1000:1000 /opt/icp-scout/data /opt/icp-scout/web
```

## 3. Data

Obraz nese kód a šest závislostí, nic víc. `res_data.csv` (517 MB),
`companies.db`, `archive.db` a snapshoty se do něj **nezapékají**: generují se
z veřejných registrů, jsou v `.gitignore` a obraz s kopií českého obchodního
rejstříku by byl obrovský a den po sestavení zastaralý. Mountují se.

Na prázdném adresáři je potřeba je nejdřív postavit:

```bash
docker compose run --rm app python -m pipeline.run --bootstrap
```

## 4. Provoz

```bash
docker compose ps
docker compose logs -f app
docker compose exec app python -m pipeline.run     # týdenní běh z terminálu
curl -s localhost:8000/api/run                     # zevnitř sítě: přes caddy
```

Týdenní běh jde spustit i tlačítkem v rozhraní; `POST /api/run` ho pustí jako
podproces **uvnitř `app`**, takže sdílí jeho paměťový strop.

### Paměť

Stroj má 2 GB a žádný swap, a druhý web na něm už něco drží. `app` má proto
`mem_limit: 1200m`. Bez stropu by přerostlý běh přivolal OOM killer jádra a to
si oběť vybírá vlastní aritmetikou — klidně sousední databázi. Se stropem
zabije jen tenhle kontejner a `restart: unless-stopped` rozhraní hned vrátí.

Naměřeno 06.09.2026 na běhu spuštěném tlačítkem: **8 min 14 s**, cgroup
`memory.peak` **733 MiB** z 1200 MiB — a v tom je i 262 MiB, které drží samotný
uvicorn. Rezerva je tedy zhruba třetina stropu.

Až sem se to dostalo přes jednu opravu. `pipeline/sources/mpsv.py` načítal
celý 186MB export do paměti a parsoval ho jedním `json.loads`: špička 804 MB
nad 186MB payloadem, dohromady kolem gigabajtu — víc, než kolik zbývá vedle
běžícího uvicornu. První běh na serveru tam dostal SIGKILL po 4 min 10 s.
Teď se pole čte po prvcích přes `raw_decode` a špička parseru je ~7 MB.

Když běh umře dvakrát na stejném místě, je to ten symptom: buď zvednout strop,
nebo v `pipeline/sources/*.py` snížit `workers` (výchozí 6–8).

## 5. Aktualizace

```bash
cd /opt/icp-scout && git pull && docker compose up -d --build
```

Data i uložené ICP (`web/icp.json`) jsou bind mounty, takže přestavbu přežijí.
