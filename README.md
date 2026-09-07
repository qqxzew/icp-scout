# icp-scout

Vezme profil zákazníka, projde veřejné české rejstříky a jednou týdně vrátí pár firem,
u kterých se **tenhle týden něco stalo** — s podkladem, ve kterém má každé tvrzení svůj
stav a zdroj.

Celý nástroj stojí na jedné větě: **model smí tvrzení navrhnout, ale faktem se stane
jen to, co kód doslova najde v uloženém textu stránky.** Zbytek se označí jako úsudek,
nebo se zahodí a spočítá.

Zatím jen pro Českou republiku — zdroje jsou české registry. Ostatní vrstvy (archiv,
ověřování citací, okna signálů, výběr) na zemi nezávisí.

**Živě běží na [icp-scout.fun](https://icp-scout.fun)** — poslední týden, filtry
i historie toho, co už jednou šlo ven. Je to ta samá instance, na kterou míří deploy
níž: co je v `main`, je za minutu tam.

Proč je to postavené takhle, do hloubky a s odkazy z kódu:
[ARCHITECTURE.md](ARCHITECTURE.md). *In English: [README.en.md](README.en.md).*

---

## Co to není

- **Není to CRM ani odesílatel.** Nic sám neodesílá. Poslední slovo má vždycky člověk.
- **Není to náhrada firemních databází.** Ty odpovídají na „jaké firmy existují".
  Tohle odpovídá na „u kterých se něco pohnulo a co o tom umím doložit".
- **Není to LLM wrapper, který si o firmě povídá.** Model nemá právo tvrdit; má právo
  navrhnout něco, co pak kód ověří proti staženému textu.
- **Není to nekonečný seznam.** Pět karet týdně je záměr, ne strop. Když jich poctivě
  vyjde míň, odevzdá se míň — okno se nerozšiřuje, aby se kvóta naplnila.

---

## Jak vypadá výstup

Tvar karty. Hodnoty vázané na konkrétní firmu jsou nahrazené zástupnými, struktura
a počítadla jsou skutečná:

```
FIRMA s.r.o.   (IČO ········)

  Sídlo             obec · okres · kraj                      ares.gov.cz/…
  Vzdálenost        85 km od zvoleného bodu                  RÚIAN
                    — pozor, provozovna je 230 km
  Velikost          100-199 zaměstnanců                      ares.gov.cz/…-res
  NACE              Výroba ostatních strojů                  ares.gov.cz/…-res
  Obrat (závěrka)   123 456 000 Kč (2025)                    or.justice.cz/…
  Web               firma.cz [proven]                        https://firma.cz
  Certifikát        ISO 9001                                 firma.cz

  Jednatel          ······ · jednatel, od 2019-04-01         ares.gov.cz/…-vr
    ↳ kanál z webu  (žádný)
  Jednatel          ······ · jednatel, od 2023-11-15         ares.gov.cz/…-vr
    ↳ kanál z webu  ······@firma.cz                          firma.cz
  Kontakt z webu    ······ · jednatel — dle webu
    ↳ kanál         ······@firma.cz · +420 ·········         firma.cz
  Kontakt firmy     prodej@firma.cz · +420 ·········         firma.cz

  Proč teď          ······ (jednatel) — opustil statutární orgán     ares.gov.cz/…-vr

  Doklad            zpracování dokumentace, výroba, povrchová úprava…
                    „doslovná věta, která se našla v archivovaném textu stránky"
                                                             https://firma.cz/

  6 ověřených faktů · 0 úsudků · 2 zahozeno při ověření · 3 nezapočteno (mimo signál)
```

**Poslední řádek je celý smysl.** Šest faktů znamená šest citací, které se doslova
našly v textu stránky uložené i s URL a datem. *Zahozeno* znamená, že model vrátil
citaci, která na stránce není — dál neprošla, ale karta o ní nemlčí. *Nezapočteno* jsou
tvrzení, která ověřením prošla a přesto se nepočítají: druhá vrstva je označila za
doslova pravdivá, ale mimo položenou otázku.

Tři mechaniky, které z toho tvaru plynou:

- **Rozpor mezi zdroji se ukazuje, neschovává.** Když rejstřík říká, že člověk odešel
  ze statutárního orgánu, a web ho pořád uvádí jako jednatele, stojí u jeho jména „dle
  webu" a odchod je zároveň tím datovaným důvodem k hovoru. Kdyby obě role měly stejný
  vzhled, vypadaly by stejně jistě.
- **Vzdálenost se měří přes všechny adresy firmy.** Sídlo je poštovní adresa,
  provozovna je místo činnosti, a který z bodů je dílna, rejstřík neříká. Dovnitř
  poloměru se firma pustí na ten bližší — a karta má povinnost vytisknout ten
  vzdálený.
- **Chybějící údaj se tiskne jako chybějící.** Kde není obrat, je napsáno proč
  („závěrka je sken bez textové vrstvy"), ne prázdno a ne odhad.

Totéž je i v prohlížeči. Rozhraní je statika bez buildu a má čtyři obrazovky. Aplikace
se otevírá **týdnem** — pěti kartami v pořadí, ve kterém je výběr seřadil. Odtud vede
odkaz na **podklad** jedné firmy, na **filtry** (profil) a na **historii**: co už jednou
šlo ven, seskupené po bězích, i s tím, kolikrát ta firma šla ven celkem. Běh se spouští
tlačítkem a stránka říká, jak je ten poslední starý.

Historie je tam ze stejného důvodu jako počítadla na kartě: bez ní se nepozná, že se
stejná firma nabízí potřetí. A firma, která mezitím z báze vypadla — rejstřík se mění,
profil se mění —, zůstává v historii jako IČO bez jména. To je stav, ne chyba, a
vynechat ji by znamenalo tvrdit, že se nikdy neodevzdala.

---

## Jádro: tvrzení modelu kontroluje kód

Halucinace je u téhle úlohy hlavní riziko. Vymyšlené tvrzení o firmě, které někdo
vloží do e-mailu, je horší než žádný výstup — a přesně tenhle způsob selhání většina
nástrojů „AI pro sales" nijak neřeší.

Tři stavy jednoho tvrzení, a rozhoduje mezi nimi **kód, ne model**:

| stav | podmínka | co je vidět na kartě |
|---|---|---|
| **fakt** | citace se našla ve staženém textu stránky | tvrzení + citace + URL + datum |
| **úsudek** | citace chybí, nebo je tvrzení z citace odvozené | označeno jako úsudek modelu |
| **zahozeno** | model vrátil citaci, která na stránce není | dál neprojde, jen se spočítá |

Model odpovídá strukturovaně (`response_format: json_schema`, `strict: true`) ve tvaru
`{tvrzení, citace}`. Kód tu citaci hledá v uloženém textu — obyčejné hledání
podřetězce, obě strany přes stejnou normalizaci. Žádné „nebuď si příliš jistý"
v promptu, protože se nedá ověřit, jestli model poslechl. Žádný druhý model jako první
instance, protože halucinuje taky. **Prompt prosí, aby se model choval správně;
architektura zařizuje, že jinak to nejde.**

Proč přesná shoda a ne fuzzy porovnání: volnější shoda by se musela ladit a není proti
čemu. Exaktní shoda po normalizaci je jediná varianta, která nepotřebuje kalibraci
a nedá se obejít parafrází.

Ověřuje se **každé tvrzení zvlášť, ne odpověď jako celek.** V jedné odpovědi modelu
běžně žijí obojí — ověřitelná data i neověřitelný úsudek.

**Co to nedokáže, řečeno nahlas.** Shoda dokazuje, že věta na stránce je — ne že
odpovídá na položenou otázku. Jeden běh dal na kartu „Jednosměnný provoz" jako doklad
rozsahu výroby, protože ta věta na stránce opravdu byla. Vedle ověřování proto stojí
dvě pojistky, které do rozhodnutí fakt / úsudek / zahození nesahají:
`verify.states_absence()` odmítá počítat bezcitační „o X se nikde nepíše" jako důkaz X,
a soudce relevance (`llm/prompts/relevance.py`) může ověřený fakt označit za
nepřípadný. **Ubírat smí, přidávat ne** — faktem se pořád stane jen to, co prošlo
hledáním v textu.

Nosný řádek schématu je `claim.snapshot_id NOT NULL REFERENCES snapshot(id)`. Tvrzení
o firmě, které neukazuje na uložený snímek stránky, se do databáze fyzicky nevloží —
„nic bez zdroje" tedy není disciplína, kterou si musí někdo pamatovat, ale cizí klíč.

### Archiv, ne cache

Cache existuje proto, aby se nestahovalo dvakrát. Tohle existuje proto, že se týden po
běhu někdo zeptá „odkud to je" a stránka už bude jiná. Text se ukládá adresovaný přes
SHA-256 obsahu, což řeší tři věci najednou: stejná stránka napříč běhy leží jednou;
„změnilo se to od minule" je porovnání dvou hashů, takže detekce změn nepotřebuje
vlastní mechanismus; a citaci nelze přišít k dokumentu, který se mezitím přepsal —
jiný text je jiný hash.

Řádově z reálného provozu: půl milionu snímků, přes 200 tisíc odlišných dokumentů,
kolem tisíce uložených tvrzení a k tomu necelé dvě stovky zahozených, které se
pamatují taky.

---

## Jak běh vypadá

Pořadí není náhoda: **levné a strukturované napřed, drahé a špinavé nakonec.**

```
1 icp       profil z rozhraní; zapíše se k běhu
2 refresh   jen to, co se opravdu pohnulo — dávky změn z rejstříku řeknou,
            kterých firem se to týká, a přenačtou se jen ty
3 gate      profil (obor, velikost, region), negativní filtry, a nakonec
            NOW: bez datované události firma končí tady
4 enrich    web, kontakty, certifikáty — VÝHRADNĚ pro ty, co prošly branou
5 agents    průchod LLM, opět jen nad těmi, co prošli
6 select    pořadí podle toho, co se skutečně doložilo
7 cards     vykreslení a zápis, co se odevzdalo
```

Drahý krok je `enrich` — obchází weby firem. Nad celou bází jsou to hodiny, nad deseti
firmami minuty. Brána proto stojí **před** ním. Není to optimalizace dodělaná potom,
je to důvod, proč se týdenní běh vejde do minut a proč volání modelu za jeden běh
stojí desetníky. (Cena se neodhaduje: každé volání se zapisuje do
`data/llm_usage.jsonl` i s tokeny a cenou.)

**Detekce změn je tažená, ne tlačená.** Nechodíme po firmách a neptáme se, jestli se
něco stalo — přijde dávka změn z rejstříku a spáruje se s bází podle IČO. Změn je
v republice za týden několik tisíc, našich z toho pár desítek. Cena tedy neroste
s velikostí báze, událost přichází s datem ze státního rejstříku a najdou se i firmy,
které v bázi ještě nejsou.

---

## „Proč teď": každý zdroj má vlastní okno

Tohle je zjištění, které vysvětlilo, proč měsíce vypadal jako funkční jen jeden signál.
Byl to jediný funkční signál — **tři zdroje ze čtyř publikují pomaleji, než se běh
opakuje.** Ptát se jich „co bylo za posledních sedm dní" znamená chtít po nich něco, co
ještě fyzicky neobsahují, a dostat zpátky nulu, která neříká nic.

| zdroj | zpoždění (naměřeno) | okno |
|---|---|---|
| obchodní rejstřík | 2 dny | okno běhu, 7 dní |
| inzeráty | 10 dní | `VACANCY_WINDOW` = 24 dní |
| dotace | 35 dní | `SUBSIDY_WINDOW` = 120 dní |
| veřejné zakázky | týž den | žádné — rozhoduje lhůta pro podání |

Šířka okna se odvozuje, nevolí: `zpoždění + kadence běhu`, a to dvakrát, aby signál
nepropadl mezi dvěma běhy. Zpoždění se přeměřuje při každém běhu a nástroj varuje,
jakmile konstanta přestane kadenci pokrývat. Měří se 1. percentil stáří záznamů, ne
nejčerstvější řádek: ten je jeden z desítek tisíc a lže třikrát.

Kolik toho takový signál unese, se dá spočítat dopředu. Zpětný test přes 104 týdnů:
medián patnáct firem s událostí týdně, z toho osm takových, které projdou podmínkou
výdeje. Alespoň pět jich vyšlo v 89 ze 104 týdnů — pětka se udrží, ale bez rezervy,
a to je argument pro další zdroje, ne pro širší okno.

---

## Kdo se odevzdá a kdo ne

Tvrdá podmínka: firma jde ven jen s **doloženou doménou** (`proven`, ne `probable`)
a **alespoň jedním kanálem**. Zeměpis je striktní: zvolený poloměr bez výjimek, měřený
přes všechny adresy firmy.

Cena je vyčíslená a placená vědomě: firma bez doložené domény se neodevzdá nikdy.
Důvod je změřený — z uhodnutých domén, které skutečně žijí, jich **46 % patří jiné
firmě**. Bez toho pravidla nosí podklad text cizí firmy a kontakt cizího člověka; to se
jednou stalo a od té doby platí tvrdý zákaz.

Firma, která podmínkou neprojde, nedostane ani řádek — její karta by nesla jméno, IČO
a jednu větu z rejstříku, což je zrovna ten seznam, kterým výstup být nemá. Počet
zadržených ale zůstává v hlavičce týdne.

Pořadí neurčuje součet skóre, ale **třída důvodu**:

```
A dotace bez zahájené zakázky   (peníze jsou, nákup nezačal)
B změna ve vedení nebo vlastnictví
C otevřená veřejná zakázka
D inzerát na řídící/plánovací roli
E jiná událost
```

Uvnitř toho se řadí: skupina profilu → negativní nález → třída důvodu → potvrzení
druhým signálem → vzdálenost → čerstvost uvnitř třídy → počet faktů jako rozhodčí
kritérium.

Nejlepší řádek **každé** třídy zabere slot dřív, než kterákoli třída obsadí druhý.
Není to rozmanitost pro rozmanitost: kalibrovat váhy není na čem, a týden z pěti
stejných důvodů je jeden pokus provedený pětkrát. Cena se říká nahlas — firma na
rezervovaném slotu je v pořadí níž než ta, kterou vytlačila, a musí to vytisknout.

---

## Zdroje

| zdroj | co dává | poznámka |
|---|---|---|
| **RES** (bulk CSV) | první výběr: velikost, obor, okres | celý registr jedním souborem, filtruje se lokálně — nula HTTP dotazů |
| **ARES**, 4 GET endpointy | identita, sídlo, insolvence, velikost, statutární orgány a vlastníci **s daty**, živnosti, provozovny | strukturovaný záznam s datem ze státního rejstříku — nejčistší signál, jaký je k dispozici |
| **ARES notifikace** | denní dávky změn — kdo se pohnul | týdenní aktualizace v řádu minut místo hodin; historie dávek ~30 dní |
| **RÚIAN** | souřadnice adresního místa | vzdálenost k nejbližší adrese firmy |
| **web firmy** | provozní signály, kontakty | adresa webu není v žádném rejstříku — hádá se a **dokazuje** (IČO, DIČ, WHOIS, adresa) |
| **WHOIS CZ.NIC** (port 43) | vlastník domény | důkazní nástroj, ne zdroj kontaktů; limit naměřen na 1 dotaz/s |
| **MPSV** | inzeráty, denní JSON + archiv přírůstků | zdroj růstového signálu |
| **dotace** | měsíční XLSX, projekty s datem podpisu | pozor: dotace se dává na projekt, ne firmě — většina projektů je o něčem jiném |
| **NEN** | veřejné zakázky | sloupec stavu rozliší „kupuje teď" od „už koupil" |
| **Sbírka listin** | obrat z účetní závěrky | **do výběru nevstupuje**, jen doplňuje kartu — čitelný výkaz má 16,5 % firem |

Dotace a zakázka jsou dva okamžiky jednoho nákupu a potřeba jsou oba:

| dotace | zakázka | význam |
|---|---|---|
| je | **není** | peníze jsou, nákup nezačal — nejzajímavější případ |
| je | otevřená | kupují, zadání už je napsané |
| je | uzavřená | koupili, pozdě |
| není | je | kupují za své |

Signál má u obou dvojí význam — příležitost s rozpočtem, nebo zákazník už sebraný
konkurencí. Nekóduje se; ukáže se s poznámkou a rozhodne člověk.

### Negativní filtry

Kategorie „formálně sedí, ale nikdy nekoupí" v žádném profilu nebývá, a přitom ušetří
nejvíc práce. Firma v insolvenci splňuje každý řádek zadání a prodat se jí nedá nic.
Vyhazuje se to **před** drahými kroky, dokud je to levné.

### Co se nepoužilo a proč

- **Klíčová slova v inzerátech jako důkaz vnitřního procesu** — změřeno na jednom
  z nich: 1,84 % záznamů a přesnost kolem nuly. Inzerát je psaný pro uchazeče, ne pro
  nás.
- **Registr smluv jako zdroj nákupů soukromých firem** — zveřejňovat musí jen veřejné
  instituce; soukromá firma se tam objeví leda jako protistrana.
- **Inspekce práce** — po firmách veřejná data neexistují, jen agregované roční zprávy.
- **Scraping za přihlášením** — nepřípustné technicky i podle podmínek služeb.
- **Placené databáze** — mimo záměr projektu.
- **Cokoli, co vyžaduje ruční práci na každou firmu** — může to být skvělý zdroj, ale
  patří na ruční dohledání už vybraných firem, ne do výběru.

---

## Skóre, které se neukázalo

Tahle sekce je tu proto, že negativní výsledek je taky výsledek a jinde se o něm mlčí.

Původně existovalo vážené skóre, které mělo firmy řadit. **Už nerozhoduje o ničem**,
a není to zjednodušení, je to změřené:

- součet z osmi členů se choval jako jeden — jediná složka nesla 62,6 % bodů
  a korelovala **+0,81** s počtem stažených stránek. Pořadí se tedy z velké části
  určovalo podle toho, čí web je největší;
- tutéž pětku dávalo 35 % z 500 náhodných vektorů vah, takže „naladěné" váhy nic
  neurčovaly;
- proti ručně označené kontrolní skupině neoddělily nic ani stavěné komponenty
  (p = 0,72–1,00), ani embedding webu v 1536 dimenzích (AUC 0,39).

Strop je ve zdroji: **z veřejného textu webu se to prostě poznat nedá.** Skóre proto
zůstalo tím, čím být poctivě může — obsahem karty, kterou čte člověk.

Jedna věc se ale změřit dala. **Potvrzení druhým signálem** — dvě různé události
najednou — bylo jediné s reálným liftem: **1,85**, tedy 24 % proti základním 13 %.
Proto je to krok v řazení a proto se počítá po druzích událostí, ne po jejich rodinách:
tak to bylo naměřeno a při hrubším počítání ta úroveň nevystřelí nikdy.

---

## Kde to má strop

Tohle patří do README, ne do issue trackeru — jsou to vlastnosti zdrojů, ne chyby
k opravení.

| co | v čem je problém |
|---|---|
| Rejstříková událost ≠ změna vedení | jediný signál rychlejší než běh — a asi třetina nálezů není to, co se zdá. Měřeno na ročním vzorku: u 28 % vlastnických změn je vlastníkem právnická osoba, tedy přesun uvnitř holdingu; 29 % příchodů do statutárního orgánu jsou lidé, kteří v záznamu té firmy už figurují. Dalších 27 % byly přeregistrace, které odfiltruje `drop_reentries()`. |
| Rejstřík vidí jen vrchol | povýšení uvnitř firmy se do rejstříku nedostane nikdy. Nejověřitelnější signál je zároveň nejméně prediktivní: medián mezi rejstříkovou událostí a nákupem vychází kolem 290 dní. |
| Zakázky nejsou na jednom místě | NEN je jen jeden z certifikovaných profilů zadavatele; ostatní zatím neobcházíme, takže část zakázek uniká. |
| Doména skupiny ≠ doména firmy | část domén je doložená přes mateřskou společnost nebo sdílená kvůli rodovému názvu. Tvrzení odtud je o skupině, ne o tom konkrétním IČO. |
| Osoba vs. obecný kanál | konkrétní člověk je dohledatelný zhruba u dvou pětin firem, u třetiny je jen podatelna a ústředna. Osobní adresa se **nedá dopočítat** ze jména, jen spárovat s napsanou — a když jsou v rejstříku dva lidé stejného jména, nespáruje se nic a karta napíše proč. |
| Přesnost kontaktů na celé bázi | ověřená jen na vzorku, který se skutečně odevzdal. Nejslabší úroveň (jméno a číslo, které spolu jen sousedí na stránce) spolehlivá není a je jako taková označená. |
| Kalibrace | není na čem ladit — ručně označených firem je pár desítek a rozdíl neukázaly. Pořadí je proto vysvětlitelné po řádcích, ne optimalizované. |
| Zpětná vazba | žádná. Nástroj ví, co odevzdal a kdy — historie to i ukazuje —, ale ne jak to dopadlo. Pořadí se proto nemá z čeho učit. |

Jedna zákonitost, která se v projektu projevila pětkrát (historický záznam vydávaný za
aktuální, chybějící deduplikace, `timeout`, který nehlídá celý přenos, slepené domény,
odmítnutí DNS resolveru pod zátěží): **každý krok, který něco firmě připisuje, se musí
kontrolovat otázkou „kolik firem dostalo tutéž odpověď".** Na jedné firmě je takový
defekt z principu neviditelný.

Zvlášť to platí o výjimkách. Timeout, `socket.gaierror` ani 403 neznamenají „firma to
nemá" — znamenají, že se zdroj neozval. Nedostupný zdroj proto vrací `None`, ne prázdný
seznam; jeden takový záměn stál doklady u dvou tisíc firem.

A jedno pozorování o metodě: dva automatické experimenty vrátily hladká čísla, zatímco
dvě hodiny s otevřenými weby našly dva defekty, oba kazily špičku výstupu. **Čtení
karet očima je nástroj, ne formalita.**

---

## Rychlý start

Python 3.14, šest připnutých závislostí, zbytek standardní knihovna.

```bash
pip install -r requirements.txt
```

Klíč k modelu do `.env` (`OPENAI_API_KEY=sk-…`) nebo do prostředí. Adresář `data/` je
celý v `.gitignore` — je odvozený a znovu stažitelný —, takže čerstvý klon nemá data
žádná. Nejdřív se zeptejte, co chybí:

```bash
python -m pipeline.run --check
```

Vypíše každý předpoklad: co to je, která fáze ho potřebuje a jestli si ho umí opatřit
sám. Co umí, postaví:

```bash
python -m pipeline.run --bootstrap
```

První stavba je zdaleka nejdelší část celého provozu — stahuje se registr a obchází se
weby. Jediné, co si nástroj obstarat neumí, je API klíč: ten pojmenuje a zastaví se.
Bootstrap, který půlku tiše zvládne, je horší než ten, který řekne, který krok je váš —
každý další soubor se odvozuje z předchozího, takže pokračovat bez toho prvního znamená
vyrobit řetěz prázdných souborů, které vypadají jako pravé. Ze stejného důvodu se
u velkých souborů nekontroluje jen „existuje": stažení, které umře v půlce, projde
každým testem existence a pak tiše vrátí zkrácený seznam.

Ten seznam předpokladů má vlastní historii, protože se mýlil na obě strany a pokaždé
potichu. První čistá stavba doběhla, ohlásila úspěch — a nechala po sobě stroj, na
kterém nešlo postavit rozhraní, protože v seznamu chyběl číselník oborů. Chybějící
soubor se zakázkami zase neshodil vůbec nic: jen se v žádném týdnu nemohl objevit důvod
třídy C a nikde nestálo proč. **Mlčení je horší selhání než pád**, takže se do seznamu
dostalo obojí.

Pak už jen:

```bash
python -m pipeline.run                               # týdenní běh
python -m pipeline.run --stage gate --stage select   # jen některé fáze
python -m pipeline.run --window 14 --top 5
```

Rozhraní — jeden proces obsluhuje statiku i `/api`:

```bash
python -m uvicorn api.main:app --port 8000
```

Data pro obrazovky staví `--bootstrap` sám; po aktualizaci registru se přepočítají
`python -m pipeline.build_ui_data`.

Každý zdroj má vlastní CLI a jde spustit samostatně, což je zároveň nejrychlejší
způsob, jak se v kódu zorientovat:

```bash
python -m pipeline.sources.ares 29092540
python -m pipeline.evidence.archive --stats
python -m pipeline.scoring.card <ičo> --no-fetch
```

### Profil se zadává v rozhraní, ne v kódu

Profil je vstup, ne konstanta — proto může být repozitář veřejný a přitom použitelný na
cizí data. Uloží se do `web/icp.json`, což je **ten samý soubor**, který čte
`pipeline/run.py`; jedno místo pro obojí. V repozitáři je jeden ukázkový profil, aby
obrazovky nezačínaly prázdné; přepíše se ve dvou krocích a další běh jede podle
nového. Prázdné pole přitom neznamená „všechno" — znamená „nikdo se ještě nerozhodl"
a použije se výchozí hodnota.

---

## Nasazení

Push do `main` a za minutu běží demo na tom commitu.

```
push → GitHub Actions → git archive | ssh → receive.sh → rsync → build → restart → smoke test
```

Celý přenos je jedna roura: `git archive` na runneru rovnou do `deploy/receive.sh` přes
ssh. Žádný registry, žádný checkout na serveru — **server tedy k tomuhle repozitáři
nepotřebuje žádné přihlašovací údaje**, a to je celý důvod, proč to není `git pull` na
druhé straně.

Rozhodnutí, která za vysvětlení stojí:

- **Deploy nikdy nespouští běh.** Týdenní běh trvá dlouho a utrácí za model, takže
  zůstává rozhodnutím člověka, ne vedlejším efektem pushnutého kódu.
- **A hlavně ho nesmí zabít.** Běh je podproces uvnitř kontejneru, takže restart by ho
  poslal k zemi. Skript se proto nejdřív zeptá, jestli něco běží; když ano, odmítne
  restartovat a skončí nenulově. Nic se neztrácí — soubory jsou nasyncované, image
  postavená, stačí deploy zopakovat, až běh doběhne. Deploy, který se neprojevil, nesmí
  svítit zeleně.
- **Klíč umí jen tohle.** V `authorized_keys` má `command="…/receive.sh"`, takže s ním
  nejde otevřít shell ani forwardovat port. Na stroji, kde běží i cizí web, je obyčejný
  deploy klíč v secretu totéž co root shell pro každého, kdo si přečte log workflow.
- **Co se nikdy nepřepisuje:** `.env`, `data/` a `web/icp.json`. První dvě v repozitáři
  nejsou vůbec; třetí ano — proto je vyloučený jmenovitě, ne doufáním. Uložený profil je
  vstup uživatele, ne build artefakt.
- **Rsync s `--delete`,** aby soubor smazaný v gitu zmizel i na serveru. Dvakrát to
  kouslo: skript rsyncuje sám sebe za běhu (bezpečné jen proto, že rsync píše dočasný
  soubor a přejmenovává ho) a napoprvé se rovnou smazal, protože na serveru existoval
  a v gitu ne. Co tenhle deploy potřebuje, musí být v gitu.
- **Smoke test vede přes Caddy**, ne přes port aplikace — to je cesta, kterou jde
  návštěvník. Kontroluje se i jedna karta, protože ten endpoint už jednou spadl tiše:
  vracel 404 na každou firmu, zatímco všechny stránky dál odpovídaly 200. Nakonec se
  totéž zeptá zvenčí přes veřejnou adresu, protože tunel nebo DNS můžou být dole, i když
  jsou všechny kontejnery zdravé.
- **`.gitattributes` vynucuje LF** u všeho, co server spouští. Píše se to na Windows,
  nasazuje na Linux a přenos je `git archive` — takže co uloží git, to bash na druhé
  straně provede, a skript s CRLF spadne na prvním řádku hláškou, která neřekne proč.

Adresa stroje a uživatel, pod kterým se přihlašuje, v repozitáři nejsou — jsou to
secrets. Doména tajná není, běží na ní ta ukázka; nasazení ji ale nikde nemá
zadrátovanou. Je to *public hostname* na tunelu, takže přidat druhou nebo tuhle přejmenovat
je editace v dashboardu, ne commit.

Zbytek — tunel, Caddy, paměťový strop kontejneru — popisuje [DEPLOY.md](DEPLOY.md).
Celý stack se vejde na malý server vedle jiné běžící aplikace.

---

## Struktura

```
pipeline/
  sources/        jeden zdroj = jeden modul za společným rozhraním
    res_bulk.py       první výběr z lokálního CSV
    ares.py           4 GET endpointy; fetch() zvlášť, parse_*() čisté funkce
    ares_notifications.py  denní dávky změn: kdo se pohnul
    coords.py         adresní kód → souřadnice
    website.py        IČO + název → doména firmy s důkazem
    whois_cz.py       registr domén, vlastník
    contacts.py       kanály k lidem, které už známe z rejstříku
    mpsv.py           inzeráty, denní JSON + archiv přírůstků
    dotace_eu.py      měsíční XLSX (čte se zipfile + re, ne openpyxl)
    nen.py            veřejné zakázky
    sbirka.py         obrat z účetní závěrky (jen karta)
    certificates.py   ISO a spol. — vydává je třetí strana, tedy ověřitelný fakt
  evidence/       JÁDRO — není to zdroj, je to kontrola všech ostatních
    archive.py        SQLite + snímky adresované přes SHA-256
    verify.py         citace v archivu? → fakt / úsudek / zahození
  signals/
    now.py            datované události „proč teď", každý zdroj s vlastním oknem
    mode.py           odvozené vlastnosti z textu
  llm/
    client.py         jedny dveře pro všechny prompty; strukturovaný výstup, cache, cena
    prompts/          jednotlivé úlohy pro model
  filters/
    brief.py          profil z rozhraní jako filtr kandidáta
    negative.py       insolvence, likvidace
  scoring/
    select.py         podmínka výdeje + pořadí + kvóta na třídu důvodu
    card.py           podklad, který čte člověk
  run.py            jeden běh celý, plus preflight
api/main.py         rozhraní a volání za ním
web/                statika bez buildu, čtyři obrazovky
  index.html          týden — na tomhle se aplikace otevírá
  brief/              profil: obor, velikost, region
  history/            co už jednou šlo ven, seskupené po bězích
  card/               podklad jedné firmy
  run-control.js      tlačítko běhu a stáří toho posledního
.github/workflows/  deploy: push do main → běžící demo
deploy/             druhá půlka nasazení: receive.sh, Caddyfile
data/               celé v .gitignore (registry, archiv, snímky)
```

---

## Přispívání

Nejužitečnější příspěvek je **nový zdroj**. Modulů bude vždycky víc než dnes, seznam je
otevřený a přidání nového se nesmí dotknout ničeho kromě vlastního souboru.

1. **Zdroj nerozhoduje.** Jenom „dojde a přinese". Kdo se odevzdá, řeší `scoring/`.
2. **HTTP odděleně od parsování.** `fetch()` chodí po síti, `parse_*()` jsou čisté
   funkce nad payloadem — dají se ladit nad uloženým JSONem bez sítě.
3. **Co se cituje, musí být v archivu.** Snímek se ukládá dřív, než z něj vznikne
   tvrzení. Bez `snapshot_id` se tvrzení nevloží.
4. **Vlastní CLI**: `python -m pipeline.sources.<jméno> <argument>` musí samo o sobě
   vypsat něco smysluplného.
5. **Prázdná odpověď není fakt.** Nedostupný zdroj vrací `None`, ne prázdný seznam.
6. **Kód anglicky** (identifikátory, komentáře, docstringy). Komentáře vysvětlují
   **proč** a krajní případy dat, ne co dělá řádek pod nimi.
7. **Jen standardní knihovna**, dokud se závislost neobhájí. XLSX se tu čte přes
   `zipfile` a `re` a je to v pořádku.

Stejně cenné jsou **měření**. Skripty `*_probe.py` v kořeni jsou přesně to: jednorázové
otázky typu „kolik toho ten zdroj vlastně obsahuje". V repozitáři je i jejich výstup, ne
jen kód — čísla v tabulkách výš se tak dají zkontrolovat, aniž by se probe pouštěl znovu
proti datům, která se mezitím pohnula. Číslo, které něco z nich vyvrátí, je vítaný pull
request.

---

## Právní rámec

**Profiluje se firma, ne člověk** — a není to formulace, je to rozdělení v datech.
Fakta, signály, hodnocení a historie visí na **IČO**. Jméno člověka žije výhradně
v bloku kontaktů: jméno, funkce, odkaz do rejstříku. K člověku se nevede žádné
hodnocení, žádná poznámka, žádná historie a nespojují se u něj zdroje.

Důvod: oprávněný zájem jako právní titul nepokrývá pokročilé profilování se slučováním
údajů o člověku z různých zdrojů. Profil o právnické osobě tenhle problém nedělá.

Proto se vyřazují fyzické osoby podnikající, adresy osobního tvaru se označují
a nástroj **nic sám neodesílá** — správcem údajů zůstává ten, kdo ho používá.

Zdroje jsou veřejné registry a veřejné weby, stahované v tempu, které servery unesou.
Nic za přihlašovací stěnou.

---

## Stav a co dál

Celý týdenní běh funguje od profilu po kartu, běží v provozu a nasazuje se pushem do
`main`. Hotový produkt to není: chybí zpětná vazba od uživatele, kalibrace pořadí
a ověření kontaktů na celé bázi.

Nejbližší směry, seřazené podle toho, kolik toho odemknou:

- **další profily zadavatele veřejných zakázek** — teď uniká část nákupů
- **referenční listy dodavatelů** jako negativní filtr: čerstvý případ znamená „už
  koupili", starý naopak důvod k hovoru
- **stav mezi běhy**: historie ukazuje, co už jednou šlo ven a kolikrát. Chybí druhá
  půlka — co s firmou, která prošla vším, ale datovaný důvod zrovna neměla
- **ověření kontaktů na celé bázi**, ne jen na tom, co prošlo ven
- **jiná země**: nová sada modulů v `sources/`, zbytek by měl zůstat

---

## Licence

[MIT](LICENSE). Kód se smí použít, upravit i prodat, jediná podmínka je nechat u něj
uvedené autorství. Záruka žádná — u nástroje, který sbírá tvrzení z cizích webů, je to
namístě říct nahlas: **ověřuje se, že věta na stránce byla, ne že je pravdivá.**

Licence se týká kódu. Data, se kterými pracuje, mají vlastní režim: veřejné registry
mají své podmínky užití, weby firem taky a zpracování osobních údajů se řídí předchozí
sekcí, ne touhle.
