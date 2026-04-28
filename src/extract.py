"""Extract structured event data from an article or primary-source
document via the LLM.

Two prompts live alongside each other:
- prompts/extract_event.txt  (news-event extraction; tier="framing")
- prompts/extract_action.txt (primary-source extraction; tier="action")

Each prompt declares its own `PROMPT VERSION:` in the leading comment
block and that version is stamped onto every extracted event for
auditability.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional, Tuple

from src.llm import GeminiClient

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
PROMPT_PATH = PROMPTS_DIR / "extract_event.txt"            # news / framing
ACTION_PROMPT_PATH = PROMPTS_DIR / "extract_action.txt"    # primary source / action

_VERSION_RE = re.compile(r"#\s*PROMPT VERSION:\s*(\S+)")


def _parse_prompt_version(text: str) -> str:
    """Extract version from the leading comment block. 'unknown' if missing."""
    for line in text.splitlines()[:10]:
        m = _VERSION_RE.search(line)
        if m:
            return m.group(1)
    return "unknown"


# Per-path cache: each prompt file is read once and its (template,
# version) memoized. This keeps the news pipeline's "load once on
# initialization" behavior intact while letting the action pipeline
# resolve its own prompt without paying the read cost per call.
_PROMPT_CACHE: dict = {}


def _get_prompt(prompt_path: Path) -> Tuple[str, str]:
    cached = _PROMPT_CACHE.get(prompt_path)
    if cached is not None:
        return cached
    text = prompt_path.read_text(encoding="utf-8")
    version = _parse_prompt_version(text)
    _PROMPT_CACHE[prompt_path] = (text, version)
    return text, version


# Eagerly resolve the news prompt's version so existing code paths
# (reextract.py, audit_versions.py) that import PROMPT_VERSION at the
# module level continue to see the news/framing version.
_PROMPT_TEMPLATE, PROMPT_VERSION = _get_prompt(PROMPT_PATH)


def _fill_prompt(template: str, article: dict) -> str:
    # Simple replacement (not str.format) because the template contains
    # JSON examples full of literal braces.
    return (
        template.replace("{{ARTICLE_SOURCE}}", str(article.get("source", "")))
        .replace("{{ARTICLE_DATE}}", str(article.get("date", "")))
        .replace("{{ARTICLE_TITLE}}", str(article.get("title", "")))
        .replace("{{ARTICLE_BODY}}", str(article.get("body", "")))
    )


def extract_event(
    article: dict,
    client: Optional[GeminiClient] = None,
    prompt_path: Path = PROMPT_PATH,
) -> dict:
    """Extract a structured event from an article-shape dict.

    article keys: source, date, title, body, url

    By default uses the news/framing prompt. Pass `prompt_path` to use
    a different extraction prompt (e.g. ACTION_PROMPT_PATH for primary-
    source documents). The returned event is stamped with the prompt
    version that produced it.
    """
    if client is None:
        client = GeminiClient()

    template, version = _get_prompt(prompt_path)
    prompt = _fill_prompt(template, article)
    event = client.extract_json(prompt)

    # Carry through provenance so downstream code can audit any event
    # back to the article it came from, and back to the prompt version
    # that produced it.
    event["source"] = article.get("source")
    event["date"] = article.get("date")
    event["url"] = article.get("url")
    event["prompt_version"] = version
    return event


def extract_action_event(
    article: dict,
    client: Optional[GeminiClient] = None,
) -> dict:
    """Extract an action-tier event using the primary-source prompt.

    Same article-shape dict as extract_event(). Callers (e.g.
    pipeline_actions.py) shape the FR document into this shape: pass
    the document type as `source`, publication date as `date`, doc
    title and body as you'd expect. The agencies list, if any, can be
    prepended to the body so the LLM sees it.
    """
    return extract_event(article, client=client, prompt_path=ACTION_PROMPT_PATH)


if __name__ == "__main__":
    contempt_article = {
        "source": "AP News",
        "date": "2026-01-15",
        "title": "Federal court holds administration in contempt over deportation orders",
        "body": (
            "A federal judge in Washington on Thursday held the Trump administration "
            "in civil contempt for failing to comply with a court order halting "
            "deportations of Venezuelan nationals under a wartime statute, marking "
            "one of the most direct judicial rebukes of the executive branch in "
            "recent memory. U.S. District Judge Marcus Holloway ruled that the "
            "Department of Homeland Security had \"willfully disregarded\" his "
            "March 12 injunction by transporting at least 137 individuals to a "
            "third-country facility while the order was in effect. The judge "
            "ordered the administration to identify by name every official "
            "involved in the decision to proceed with the flights and gave the "
            "Department of Justice fourteen days to show cause why criminal "
            "contempt proceedings should not follow.\n\n"
            "The ruling stems from an emergency order Holloway issued last month "
            "after immigration advocacy groups argued the administration was "
            "invoking the Alien Enemies Act of 1798 to bypass standard removal "
            "proceedings. Court filings show that Justice Department lawyers were "
            "informed of the injunction by phone while the deportation flights "
            "were still in the air; according to the ruling, the planes were not "
            "recalled. \"The court does not lightly conclude that an arm of the "
            "executive branch has acted in defiance of a judicial order,\" "
            "Holloway wrote in a 47-page opinion, \"but the record permits no "
            "other interpretation.\"\n\n"
            "The White House called the ruling \"a politically motivated overreach\" "
            "and said it would appeal to the D.C. Circuit. Attorney General "
            "Pamela Bondi, in a statement Thursday evening, said the "
            "administration would \"defend its lawful exercise of statutory "
            "authority at every level.\" Constitutional law scholars at "
            "Georgetown and NYU described the contempt finding as the most "
            "serious confrontation between a federal court and a sitting "
            "president since the Watergate-era tapes case, and warned that any "
            "criminal referral would put the Justice Department in the "
            "unprecedented position of prosecuting its own client."
        ),
        "url": "https://apnews.com/example",
    }

    # Deliberately mixed-framing test: WSJ-style report on tariffs with
    # quotes from both supporters and critics, real economic data, and
    # framing language ("strategic", "punitive", "necessary corrective")
    # on both sides. Stresses the framing-bucket logic and tests that
    # neutral_summary stays clean.
    tariff_article = {
        "source": "Wall Street Journal",
        "date": "2026-02-10",
        "title": "Administration's 35% Chinese EV component tariff takes effect",
        "body": (
            "The Trump administration's 35 percent tariff on Chinese-made "
            "electric vehicle batteries and lithium components took effect "
            "Monday, marking the latest escalation in a sustained push to "
            "reshore advanced manufacturing. Commerce Department data show "
            "affected imports totaled $14.2 billion in 2025, roughly 28 "
            "percent of all U.S. EV component imports.\n\n"
            "Supporters cast the measure as a strategic correction. \"This "
            "is the necessary corrective our manufacturing base has needed "
            "for two decades,\" said Senator Marsha Carter (R-Ohio), "
            "pointing to Labor Department figures showing 47,000 "
            "manufacturing jobs returned to the upper Midwest over the past "
            "year. The American Manufacturing Coalition called the move "
            "\"long overdue\" and projected an additional 80,000 domestic "
            "jobs over five years.\n\n"
            "Critics argued the tariffs function as a regressive consumer "
            "tax. The Peterson Institute for International Economics "
            "estimated the duties would add an average of $1,840 to the "
            "price of new electric vehicles and slow EV adoption by "
            "roughly 11 percent through 2027. \"Punitive measures of this "
            "scope rarely produce the outcomes their architects promise,\" "
            "economist Maya Chen wrote in a Tuesday analysis. The "
            "Alliance for Automotive Innovation, representing Ford, GM, "
            "and Hyundai among others, warned of supply-chain disruptions "
            "and projected up to 23,000 near-term industry layoffs.\n\n"
            "Markets reacted unevenly. Shares of Tesla and Rivian closed "
            "up 2.4 percent and 1.8 percent, respectively, while suppliers "
            "Magna International and Aptiv fell 3.1 percent and 2.7 "
            "percent. The dollar weakened slightly against the yuan.\n\n"
            "Treasury Secretary Janet Smith said the administration was "
            "prepared to \"absorb short-term adjustment costs in service "
            "of long-term industrial sovereignty.\" China's Commerce "
            "Ministry called the tariffs \"discriminatory\" and indicated "
            "reciprocal measures were under review."
        ),
        "url": "https://wsj.com/example",
    }

    # Same hypothetical contempt finding written in three different
    # editorial voices. Tests whether categories and impact_magnitude
    # stay stable across outlets (they should — same underlying event)
    # while framing_indicators buckets shift to reflect each outlet's
    # framing choices.
    contempt_ap_article = {
        "source": "AP News",
        "date": "2026-01-15",
        "title": "Federal judge holds Trump administration in contempt over deportations",
        "body": (
            "A federal judge in Washington on Thursday held the Trump "
            "administration in civil contempt for failing to comply with a "
            "court order halting deportations of Venezuelan nationals.\n\n"
            "U.S. District Judge Marcus Holloway ruled that the Department "
            "of Homeland Security had not complied with his March 12 "
            "injunction after at least 137 individuals were transported to "
            "a third-country facility while the order was in effect. In a "
            "47-page opinion, Holloway gave the Department of Justice 14 "
            "days to show cause why criminal contempt proceedings should "
            "not be initiated, and ordered the administration to identify "
            "officials involved in authorizing the deportation flights.\n\n"
            "Court filings indicate that Justice Department lawyers were "
            "notified of the injunction by phone while the flights were "
            "airborne. The case stems from emergency litigation filed by "
            "immigration advocacy groups challenging the administration's "
            "invocation of the Alien Enemies Act of 1798. Holloway issued "
            "the original injunction last month, finding plaintiffs likely "
            "to succeed on procedural due process claims.\n\n"
            "A White House spokesperson said the administration disagreed "
            "with the ruling and would appeal to the D.C. Circuit. "
            "Attorney General Pamela Bondi said in a statement that the "
            "administration would defend its exercise of statutory "
            "authority. Constitutional law scholars at several "
            "universities have compared the situation to the Nixon-era "
            "tapes case in terms of judicial-executive confrontation.\n\n"
            "The Department of Homeland Security did not respond to a "
            "request for comment."
        ),
        "url": "https://apnews.com/contempt-ruling",
    }

    contempt_fox_article = {
        "source": "Fox News",
        "date": "2026-01-15",
        "title": "Activist judge holds Trump administration in contempt over deportation of Venezuelan gang members",
        "body": (
            "A federal judge in Washington on Thursday handed down a "
            "controversial ruling holding the Trump administration in "
            "civil contempt over its deportation of Venezuelan gang "
            "members under a long-standing wartime statute, igniting "
            "fierce White House pushback against what administration "
            "officials called judicial overreach.\n\n"
            "U.S. District Judge Marcus Holloway, an Obama appointee, "
            "ruled that the Department of Homeland Security violated his "
            "March 12 injunction by deporting 137 individuals — many of "
            "them members of the Tren de Aragua transnational criminal "
            "organization — to a third-country facility. The "
            "administration has invoked the Alien Enemies Act of 1798, an "
            "authority on the books since John Adams's presidency, to "
            "expedite removals of foreign nationals deemed a national "
            "security threat.\n\n"
            "The White House blasted the ruling as \"a politically "
            "motivated overreach,\" with the press secretary calling "
            "Holloway's order \"the latest attempt by activist judges to "
            "obstruct the will of the American people.\" Attorney General "
            "Pamela Bondi vowed in a statement that the administration "
            "would \"defend its lawful exercise of statutory authority at "
            "every level,\" and announced an immediate appeal to the D.C. "
            "Circuit.\n\n"
            "The contempt finding gives the Justice Department 14 days to "
            "show cause why criminal proceedings should not follow — a "
            "window administration allies described as a fishing "
            "expedition.\n\n"
            "Republican lawmakers rallied to the administration's "
            "defense. \"Federal judges do not get to write immigration "
            "policy,\" said Senator Tom Reilly (R-Tenn.). DHS figures "
            "show 47 percent of the deported individuals had prior "
            "criminal records."
        ),
        "url": "https://foxnews.com/contempt-ruling",
    }

    contempt_wapo_article = {
        "source": "Washington Post",
        "date": "2026-01-15",
        "title": "In open defiance of court order, administration faces historic contempt finding",
        "body": (
            "The Trump administration's open defiance of a federal court "
            "order halting deportations under an obscure 18th-century "
            "wartime statute moved the country into uncharted "
            "constitutional territory Thursday, as a federal judge held "
            "the executive branch in civil contempt — a sanction that "
            "legal scholars described as the most serious "
            "judicial-executive confrontation since the Watergate-era "
            "tapes case.\n\n"
            "U.S. District Judge Marcus Holloway, in a stinging 47-page "
            "opinion, found that the Department of Homeland Security had "
            "\"willfully disregarded\" his March 12 injunction by "
            "deporting 137 individuals to a third-country facility while "
            "the order was in effect. Court filings revealed that Justice "
            "Department lawyers were informed of the injunction by phone "
            "while the deportation flights were still airborne; the "
            "planes were not recalled.\n\n"
            "\"The court does not lightly conclude that an arm of the "
            "executive branch has acted in defiance of a judicial "
            "order,\" Holloway wrote, \"but the record permits no other "
            "interpretation.\"\n\n"
            "The ruling gives the Department of Justice 14 days to show "
            "cause why criminal contempt proceedings should not follow — "
            "a step that constitutional scholars at Georgetown and NYU "
            "warned would put the Justice Department in the unprecedented "
            "position of prosecuting its own client.\n\n"
            "The White House dismissed the ruling as \"politically "
            "motivated,\" signaling continued resistance. Civil liberties "
            "advocates said the case raises fundamental questions about "
            "the limits of executive authority, the durability of "
            "judicial review, and the future of due process for "
            "non-citizens facing summary removal under wartime statutes "
            "last invoked in the 1940s."
        ),
        "url": "https://washingtonpost.com/contempt-ruling",
    }

    client = GeminiClient()
    cases = [
        ("CASE 1: Federal court contempt finding (original; long AP-style)",
         contempt_article),
        ("CASE 2: Mixed-framing tariff report (WSJ)",
         tariff_article),
        ("CASE 3a: Same contempt event - AP-style neutral wire copy",
         contempt_ap_article),
        ("CASE 3b: Same contempt event - Fox News-style emphasizing administration pushback",
         contempt_fox_article),
        ("CASE 3c: Same contempt event - Washington Post-style emphasizing constitutional concern",
         contempt_wapo_article),
    ]

    for label, article in cases:
        print("=" * 72)
        print(label)
        print("=" * 72)
        result = extract_event(article, client=client)
        print(json.dumps(result, indent=2))
        print()
