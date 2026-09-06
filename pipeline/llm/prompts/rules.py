"""The quoting rules every agent shares, in one place.

Written after measuring what actually gets discarded rather than from
first principles. data/discard_probe.py takes every discard in the
archive, finds the longest stretch of it that IS present in the snapshot
it was checked against, and sorts by that fraction. On the 16 discards of
runs 150-151:

    spliced (internal ellipsis)          7  (44 %)
    invented (<50 % of it on the page)   8  (50 %)
    near miss (>=90 %)                   1  ( 6 %)

So the verifier was right 15 times out of 16 - this is not a matching
problem to be loosened away, it is the model paraphrasing. The three
prompts that ask for quotes each said so in their own words, and two of
the three said less than the third: production_mode.py, whose rule was
the shortest, produced 7 of the 16.

What the measured failures look like, and which rule below answers each:

* the model turns a LIST into one quote - six job titles joined with
  semicolons, three plant addresses run together - because the page
  presents them as a group and it is summarising the group. Nothing in
  "do not join two non-adjacent sentences" told it that a list is that.
  -> the rule about lists, named explicitly.
* the model ends a quote with a full stop the page does not have.
  Discard 172 was 265 of 266 characters verbatim and failed on a final
  "." where the source has a comma. Same instinct as the quotation marks
  verify.py already strips: it is punctuating its own citation.
  -> the rule about adding nothing, with the full stop named.
* long quotes fail far more often than short ones - every discard over
  120 characters in the sample was a paraphrase of a passage rather than
  a copy of a sentence.
  -> the rule preferring the shortest sufficient quote.

These are asked for, not enforced; enforcement stays in
evidence/verify.py, which is the whole point of the architecture. A
prompt that asks nicely and a verifier that checks are not alternatives
to each other - the prompt lowers the discard rate, the verifier makes
the remainder harmless.
"""

# Czech, because the prompts are Czech and the model answers in the
# language it is addressed in. Kept as one block so a change lands in
# every agent at once - the three copies this replaced had drifted into
# three different rules.
QUOTE_RULES = """\
Pravidla pro pole "quote", platí pro každý nález:
- Citace je JEDEN souvislý úsek textu, zkopírovaný znak po znaku od \
prvního do posledního znaku. Ne parafráze, ne shrnutí, ne vlastními slovy.
- Nikdy nespojuj dvě různá místa textu do jedné citace - ani třemi \
tečkami, ani středníkem, ani čárkou. Výčet (seznam pozic, seznam \
provozoven, seznam služeb) NENÍ jedna citace: pokud potřebuješ dvě \
položky, vrať dva samostatné nálezy, každý se svou citací.
- Nepřidávej nic, co na tom místě v textu není: žádné uvozovky na \
začátku a konci, žádnou tečku na konci. Když úsek v textu končí čárkou, \
tvá citace končí čárkou.
- Kratší citace je lepší než delší. Vyber nejkratší úsek, který tvrzení \
doloží - čím delší citace, tím větší šance, že do ní vlastními slovy \
něco doplníš.
- Když tvrzení nemáš doložené jednou konkrétní větou, je to úsudek: \
nastav "quote" na null. Úsudek je platná odpověď, vymyšlená citace ne."""
