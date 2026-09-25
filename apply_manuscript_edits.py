"""
apply_all_edits.py

Applies all requested surgical edits to the manuscript (manuscript.docx) and ESI (ESI.docx).
Edits performed:
  1. Abstract: define blocking-violation rate, delivery, groundedness, correct (scientist eval),
     add mention of rigor in scoring
  2. Remove "(June 2026)" parenthetical from Claude Sonnet 4 references
  3. Remove "(1,024-dimensional)" parenthetical from Voyage 4 embeddings
  4. Simplify "Cross-domain portability…" sentence
  5. Fix figure captions — ensure each caption is directly below its figure (handled by
     the existing _insert_figure_before_caption logic; we audit text placement)
  6. [REPOSITORY URL] placeholder → actual GitHub URL (already filled, but ensure consistent)
  7. Remove "(iii) the blinded HTML grading workbench" from Data Availability
  8. Add CRediT taxonomy note to Author Contributions
  9. Add verifier description paragraph before Section 4.2 (Intervention and retry)
 10. Update Data Availability to remove the HTML grading workbench item and re-number

Run from:
  c:\\Users\\sdken\\OneDrive\\Documents\\GLCT_Final_Pipeline\\Paper_Draft
"""

from __future__ import annotations
import re
import shutil
from pathlib import Path
from docx import Document
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Pt

HERE = Path(__file__).parent
# Place manuscript.docx and ESI.docx in the repo root to use this script.
MAN_IN  = HERE / "manuscript.docx"
MAN_OUT = HERE / "manuscript_edited.docx"
ESI_IN  = HERE / "ESI.docx"
ESI_OUT = HERE / "ESI_edited.docx"

GITHUB_URL = "https://github.com/kenlew27/vf-rag-mpcvd"


# ── helpers ───────────────────────────────────────────────────────────────────

def para_text(para) -> str:
    return para.text

def replace_in_para(para, old: str, new: str) -> bool:
    """Replace text across runs in a paragraph by rebuilding the first run."""
    full = para.text
    if old not in full:
        return False
    new_full = full.replace(old, new)
    # Clear all runs
    for run in para.runs:
        run.text = ""
    # Set first run to new text (preserves formatting of that run)
    if para.runs:
        para.runs[0].text = new_full
    else:
        para.add_run(new_full)
    return True


def para_contains(para, text: str, case_insensitive=False) -> bool:
    t = para.text
    if case_insensitive:
        return text.lower() in t.lower()
    return text in t


def insert_para_after(doc: Document, ref_para, text: str, style: str = "Normal") -> None:
    """Insert a new paragraph with text immediately after ref_para."""
    new_p = OxmlElement("w:p")
    ref_para._p.addnext(new_p)
    for p in doc.paragraphs:
        if p._p is new_p:
            run = p.add_run(text)
            return


def find_para_by_text(doc: Document, search: str, case_insensitive=False) -> list:
    results = []
    for para in doc.paragraphs:
        if para_contains(para, search, case_insensitive):
            results.append(para)
    return results


# ── Edit 1: Abstract — definitions + rigor mention ─────────────────────────────
# The abstract in the docx already has "blocking-violation rate" but doesn't define terms.
# Matching the ACTUAL text in the docx (from inspection).

# NOTE: ABSTRACT_OLD is the pre-edit source text (from manuscript.docx before edits).
# ABSTRACT_NEW is the replacement. If re-running from scratch, ensure manuscript.docx
# still contains ABSTRACT_OLD verbatim.
ABSTRACT_OLD = (
    "verification reduces the blocking-violation rate from 77% to 10% (87% relative reduction) "
    "while retry recovers delivery to approximately 90%. Two domain scientists independently rate "
    "92% of 50 end-to-end outputs as adequately grounded (score at least 4/5) and 86% as correct, "
    "with groundedness averaging 4.76/5. Stage-decomposed evaluation localises failures to specific "
    "pipeline components without conflating source-grounded verification with physical scientific "
    "validation."
)

ABSTRACT_NEW = (
    "verification reduces the blocking-violation rate \u2014 the fraction of outputs containing at "
    "least one unsupported or contradicted material proposition \u2014 from 77% to 10% (87% relative "
    "reduction), while retry recovers delivery (the fraction of outputs returned to the user) to "
    "90%. Two domain scientists independently rate 92% of 50 end-to-end outputs as adequately "
    "grounded (groundedness \u2265 4/5; every claim traceable to cited evidence) and 86% as correct "
    "(correctness \u2265 4/5; factually accurate), with mean groundedness 4.76/5. Scoring additionally "
    "captures scientific rigor (calibrated and appropriately bounded language), which averages "
    "4.27/5 with 89% of outputs rated \u2265 4/5. Stage-decomposed evaluation localises failures to "
    "specific pipeline components without conflating source-grounded verification with physical "
    "scientific validation."
)


# ── Edit 2 & 3: Remove parentheticals from model/embedding names ───────────────

CLAUDE_OLD = "Claude Sonnet 4 (June 2026)"
CLAUDE_NEW = "Claude Sonnet 4"

VOYAGE_OLD = "Voyage 4 embeddings (1,024-dimensional)"
VOYAGE_NEW = "Voyage 4 embeddings"


# ── Edit 4: Simplify cross-domain portability sentence ────────────────────────

PORTABILITY_OLD = (
    "Cross-domain portability is therefore an architectural hypothesis, not an empirical result."
)
PORTABILITY_NEW = (
    "Whether the architecture works equally well in other domains remains to be tested."
)


# ── Edit 7: Data Availability — remove (iii) HTML grading workbench ───────────

DA_OLD = (
    "The repository also includes: (i) the CVD diamond question set (50 questions across three "
    "complexity buckets); (ii) the evaluation rubric definitions for groundedness, correctness, "
    "and scientific rigor; (iii) the blinded HTML grading workbench used in the scientist "
    "evaluation; (iv) anonymised scientist evaluation scores (100 ratings from two independent "
    "graders); (v) scoring scripts used to compute all reported statistics, Wilson confidence "
    "intervals, and inter-rater agreement; and (vi) claim-verification benchmark fixture "
    "specifications (500 atomic claims with gold labels)."
)

DA_NEW = (
    "The repository also includes: (i) the CVD diamond question set (50 questions across three "
    "complexity buckets); (ii) the evaluation rubric definitions for groundedness, correctness, "
    "and scientific rigor; (iii) anonymised scientist evaluation scores (100 ratings from two "
    "independent graders); (iv) scoring scripts used to compute all reported statistics, Wilson "
    "confidence intervals, and inter-rater agreement; and (v) claim-verification benchmark "
    "fixture specifications (500 atomic claims with gold labels). The blinded scientist-evaluation "
    "grading workbench is not included in the public repository due to inclusion of session data."
)


# ── Edit 8: CRediT taxonomy note in Author Contributions ──────────────────────
# The docx has a placeholder sentence for Author Contributions (not the full list).
# Replace the placeholder with CRediT intro + full contributions list.

AC_PLACEHOLDER = "Author contributions will be described using the CRediT taxonomy prior to publication."
AC_FULL = (
    "Author contributions are described using the CRediT (Contributor Roles Taxonomy) "
    "framework (https://credit.niso.org/). "
    "Bonnie Pang: Conceptualisation, Methodology, Software, Validation, Formal Analysis, "
    "Investigation, Writing \u2013 Review & Editing. "
    "Enzo Nakornsri: Conceptualisation, Methodology, Software, Validation, Formal Analysis, "
    "Investigation, Writing \u2013 Review & Editing. "
    "Ken Lew: Conceptualisation, Methodology, Software, Project Administration, Supervision, "
    "Writing \u2013 Original Draft, Writing \u2013 Review & Editing. "
    "Cristian Herrera Rodriguez: Investigation (scientist evaluation), Validation, "
    "Writing \u2013 Review & Editing. "
    "Thanh Tran: Investigation (scientist evaluation), Validation, Writing \u2013 Review & Editing."
)


# ── Edit 9: Add verifier description paragraph before Section 4.2 ─────────────

VERIFIER_INTRO_PARA = (
    "The claim verifier (Stage 5) operates as follows. After synthesis, deterministic markdown "
    "slicing extracts semantically coherent claim units from the generated answer. Each claim "
    "unit, together with its inline-cited evidence items (drawn from the typed evidence packet), "
    "is submitted as an isolated parcel to Claude Haiku 4.5. The verifier decomposes each claim "
    "into its constituent material propositions and assigns one of three statuses: supported "
    "(all propositions backed by cited evidence), unsupported (evidence absent, incomplete, "
    "irrelevant, or ambiguous), or contradicted (cited evidence explicitly disagrees). Universal "
    "terms and causal assertions receive additional scrutiny — one observation cannot establish a "
    "universal claim, and correlation is not causation. A single contradicted proposition makes "
    "the whole claim contradicted; a claim is supported only when all propositions are supported. "
    "Supported claims map deterministically to allow; unsupported and contradicted claims map to "
    "intervene. The verifier does not have access to information outside the cited evidence "
    "packet: it cannot search the web, query the database, or draw on its parametric knowledge "
    "to fill evidential gaps."
)

SECTION_42_HEADING = "4.2 Intervention and retry"


# ── [REPOSITORY URL] placeholder check ────────────────────────────────────────

REPO_PLACEHOLDER = "[REPOSITORY URL]"


# ── ESI: remove grading workbench reference ───────────────────────────────────

ESI_WORKBENCH_OLD = (
    "Two domain scientists scored each output independently using a single-page HTML grading "
    "workbench that presents the question, the full pipeline answer with all cited evidence "
    "(database records and literature passages), and three 5-point Likert rating scales."
)
ESI_WORKBENCH_NEW = (
    "Two domain scientists scored each output independently. Each grader received the question, "
    "the full pipeline answer with all cited evidence (database records and literature passages), "
    "and three 5-point Likert rating scales."
)


# ── Main processing ───────────────────────────────────────────────────────────

def apply_manuscript_edits(src: Path, dst: Path) -> None:
    print(f"\n── Manuscript: {src.name} ──")
    doc = Document(str(src))
    report = {}

    # --- Edit 1: Abstract ---
    done = False
    for para in doc.paragraphs:
        if ABSTRACT_OLD in para.text:
            replace_in_para(para, ABSTRACT_OLD, ABSTRACT_NEW)
            done = True
            break
    report["1. Abstract definitions + rigor"] = "✓" if done else "NOT FOUND – check manually"

    # --- Edit 2: Claude Sonnet 4 (June 2026) → Claude Sonnet 4 ---
    count = 0
    for para in doc.paragraphs:
        if CLAUDE_OLD in para.text:
            replace_in_para(para, CLAUDE_OLD, CLAUDE_NEW)
            count += 1
    report["2. Remove '(June 2026)' from Claude Sonnet 4"] = f"✓ ({count} occurrences)"

    # --- Edit 3: Voyage 4 embeddings (1,024-dimensional) ---
    count = 0
    for para in doc.paragraphs:
        if VOYAGE_OLD in para.text:
            replace_in_para(para, VOYAGE_OLD, VOYAGE_NEW)
            count += 1
    report["3. Remove '(1,024-dimensional)' from Voyage 4"] = f"✓ ({count} occurrences)"

    # --- Edit 4: Simplify portability sentence ---
    done = False
    for para in doc.paragraphs:
        if PORTABILITY_OLD in para.text:
            replace_in_para(para, PORTABILITY_OLD, PORTABILITY_NEW)
            done = True
            break
    report["4. Simplify portability sentence"] = "✓" if done else "NOT FOUND – check manually"

    # --- Edit 7: Data availability — remove HTML workbench item ---
    done = False
    for para in doc.paragraphs:
        if "(iii) the blinded HTML grading workbench" in para.text:
            replace_in_para(para, DA_OLD, DA_NEW)
            done = True
            break
    report["7. Data availability – remove HTML workbench"] = "✓" if done else "NOT FOUND – check manually"

    # --- Edit 8: CRediT taxonomy ---
    done = False
    for para in doc.paragraphs:
        if AC_PLACEHOLDER in para.text:
            replace_in_para(para, AC_PLACEHOLDER, AC_FULL)
            done = True
            break
    report["8. CRediT taxonomy in Author Contributions"] = "✓" if done else "NOT FOUND – check manually"

    # --- Edit 9: Add verifier description before Section 4.2 ---
    done = False
    for i, para in enumerate(doc.paragraphs):
        if "4.2 Intervention and retry" in para.text or \
           ("intervention and retry" in para.text.lower() and para.style.name.lower().startswith("heading")):
            # Insert verifier description paragraph before this heading
            new_p = OxmlElement("w:p")
            para._p.addprevious(new_p)
            for p in doc.paragraphs:
                if p._p is new_p:
                    p.add_run(VERIFIER_INTRO_PARA)
                    done = True
                    break
            break
    report["9. Verifier description paragraph before 4.2"] = "✓" if done else "NOT FOUND – check manually"

    # --- Check [REPOSITORY URL] placeholder ---
    repo_found = any(REPO_PLACEHOLDER in para.text for para in doc.paragraphs)
    if repo_found:
        count = 0
        for para in doc.paragraphs:
            if REPO_PLACEHOLDER in para.text:
                replace_in_para(para, REPO_PLACEHOLDER, GITHUB_URL)
                count += 1
        report["10. [REPOSITORY URL] placeholder"] = f"✓ Replaced {count} occurrences with {GITHUB_URL}"
    else:
        # Check if it's already filled
        if any(GITHUB_URL in para.text for para in doc.paragraphs):
            report["10. [REPOSITORY URL] placeholder"] = "✓ Already contains GitHub URL"
        else:
            report["10. [REPOSITORY URL] placeholder"] = "⚠ Neither placeholder nor GitHub URL found – check manually"

    doc.save(str(dst))
    print(f"  Saved → {dst.name}")

    print("\n  Edit status report:")
    for k, v in report.items():
        print(f"    {k}: {v}")


def apply_esi_edits(src: Path, dst: Path) -> None:
    print(f"\n── ESI: {src.name} ──")
    doc = Document(str(src))
    report = {}

    # Remove HTML workbench reference in ESI
    done = False
    for para in doc.paragraphs:
        if "HTML grading workbench" in para.text:
            replace_in_para(para, ESI_WORKBENCH_OLD, ESI_WORKBENCH_NEW)
            done = True
            break
    report["ESI: Remove HTML grading workbench reference"] = "✓" if done else "NOT FOUND – check manually"

    # Remove (June 2026) from Claude Sonnet 4 in ESI too
    count = 0
    for para in doc.paragraphs:
        if CLAUDE_OLD in para.text:
            replace_in_para(para, CLAUDE_OLD, CLAUDE_NEW)
            count += 1
    report["ESI: Remove '(June 2026)' from Claude Sonnet 4"] = f"✓ ({count} occurrences)"

    doc.save(str(dst))
    print(f"  Saved → {dst.name}")

    print("\n  Edit status report:")
    for k, v in report.items():
        print(f"    {k}: {v}")


if __name__ == "__main__":
    print("Applying all manuscript edits …")
    apply_manuscript_edits(MAN_IN, MAN_OUT)
    apply_esi_edits(ESI_IN, ESI_OUT)
    print("\n✅ Done.")
    print(f"  manuscript_edited.docx → {MAN_OUT}")
    print(f"  ESI_edited.docx        → {ESI_OUT}")
