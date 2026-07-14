"""Seed the default global assessment questionnaire (PRD §3.4.1).

Idempotent: skips seeding when any assessment question already exists.

Run with:
    uv run python -m scripts.seed_assessment_questions
"""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from app.core.logging import get_logger
from app.db.session import async_session_factory, engine
from app.models.assessment import (
    AssessmentQuestion,
    AssessmentQuestionOption,
    QuestionCategory,
)

logger = get_logger(__name__)

# (category, text, weight, [(option_text, score, improvement_tip), ...])
DEFAULT_QUESTIONS: list[
    tuple[QuestionCategory, str, float, list[tuple[str, float, str | None]]]
] = [
    (
        QuestionCategory.nationality,
        "Do you hold a passport valid for at least 6 months beyond your intended travel date?",
        1.5,
        [
            ("Yes", 100, None),
            (
                "No, it expires sooner",
                30,
                "Renew your passport before applying — most countries require 6 months validity.",
            ),
            (
                "I don't have a passport",
                0,
                "Apply for a passport first; it is required for any visa application.",
            ),
        ],
    ),
    (
        QuestionCategory.travel_history,
        "Have you travelled internationally in the last 5 years?",
        1.0,
        [
            ("Yes, multiple trips", 100, None),
            ("Yes, one or two trips", 75, None),
            (
                "No international travel",
                40,
                "Travel history strengthens applications; consider visa-free trips first.",
            ),
        ],
    ),
    (
        QuestionCategory.travel_history,
        "Have you ever overstayed a visa or violated immigration conditions?",
        2.0,
        [
            ("Never", 100, None),
            (
                "Yes, once (minor)",
                30,
                "Disclose the overstay honestly with evidence of compliance since.",
            ),
            (
                "Yes, more than once",
                5,
                "Multiple violations are a serious barrier; seek professional advice.",
            ),
        ],
    ),
    (
        QuestionCategory.financial,
        "Can you show sufficient funds for your stay (bank statements, sponsorship, or income)?",
        1.5,
        [
            ("Yes, comfortably above the requirement", 100, None),
            (
                "Yes, but close to the minimum",
                60,
                "Build a larger financial buffer or add a sponsor to strengthen your application.",
            ),
            (
                "Not currently",
                10,
                "Most visas require proof of funds — save up or arrange sponsorship.",
            ),
        ],
    ),
    (
        QuestionCategory.financial,
        "Do you have 6+ months of consistent bank statement history?",
        1.0,
        [
            ("Yes", 100, None),
            (
                "Partially (3–6 months)",
                60,
                "Maintain consistent banking activity for at least 6 months before applying.",
            ),
            ("No", 25, "Open a bank account and build a documented financial history."),
        ],
    ),
    (
        QuestionCategory.education,
        "What is your highest completed education level?",
        1.0,
        [
            ("Master's degree or higher", 100, None),
            ("Bachelor's degree", 85, None),
            (
                "High school / secondary",
                55,
                "Further qualifications can improve points-based visa scores.",
            ),
            (
                "Below secondary",
                30,
                "Consider vocational certifications recognised by your destination country.",
            ),
        ],
    ),
    (
        QuestionCategory.employment,
        "What is your current employment situation?",
        1.5,
        [
            ("Employed full-time (2+ years with employer)", 100, None),
            ("Employed less than 2 years", 75, None),
            (
                "Self-employed with documented income",
                70,
                "Prepare audited accounts/tax returns for self-employment.",
            ),
            ("Student", 60, None),
            (
                "Unemployed",
                20,
                "Stable employment or enrolment significantly improves visa outcomes.",
            ),
        ],
    ),
    (
        QuestionCategory.criminal_record,
        "Do you have any criminal convictions?",
        2.0,
        [
            ("None", 100, None),
            (
                "Minor offence (spent/expunged)",
                50,
                "Obtain police clearance and legal advice on disclosure.",
            ),
            (
                "Serious or recent conviction",
                5,
                "A conviction may need a waiver or bar entry — consult an advisor.",
            ),
        ],
    ),
    (
        QuestionCategory.visa_refusals,
        "Have you ever been refused a visa by any country?",
        1.5,
        [
            ("Never", 100, None),
            (
                "Yes, once — different country",
                55,
                "Address the refusal reason head-on in your new application.",
            ),
            (
                "Yes, by this destination country",
                25,
                "A prior refusal from this country needs careful handling.",
            ),
        ],
    ),
    (
        QuestionCategory.family_ties,
        "Do you have strong ties to your home country (property, family, ongoing employment)?",
        1.0,
        [
            ("Yes, multiple strong ties", 100, None),
            (
                "Some ties",
                65,
                "Document home ties — deeds, family registration, work letters.",
            ),
            (
                "Few or none",
                30,
                "Weak home ties raise overstay concerns; evidence return intent.",
            ),
        ],
    ),
    (
        QuestionCategory.language,
        "What is your proficiency in the destination country's primary language?",
        1.0,
        [
            ("Fluent / native (or certified test passed)", 100, None),
            (
                "Conversational",
                70,
                "Take an approved language test — certified scores earn points.",
            ),
            (
                "Basic or none",
                35,
                "Start a language course and schedule a certified test.",
            ),
        ],
    ),
    (
        QuestionCategory.purpose,
        "How clearly can you document the purpose of your visit?",
        1.0,
        [
            ("Fully documented (offer letter, admission, itinerary)", 100, None),
            (
                "Partially documented",
                60,
                "Secure missing documents — undocumented intent causes refusals.",
            ),
            (
                "Not yet documented",
                25,
                "Get concrete evidence of purpose (invitation, enrolment, booking).",
            ),
        ],
    ),
]


async def seed() -> int:
    async with async_session_factory() as session:
        existing = await session.scalar(select(func.count(AssessmentQuestion.id)))
        if existing:
            logger.info("seed_skipped", existing=existing)
            return 0

        for order, (category, text, weight, options) in enumerate(DEFAULT_QUESTIONS):
            session.add(
                AssessmentQuestion(
                    text=text,
                    category=category,
                    weight=weight,
                    display_order=order,
                    options=[
                        AssessmentQuestionOption(
                            text=opt_text,
                            score=score,
                            improvement_tip=tip,
                            display_order=i,
                        )
                        for i, (opt_text, score, tip) in enumerate(options)
                    ],
                )
            )
        await session.commit()
        logger.info("seed_complete", count=len(DEFAULT_QUESTIONS))
        return len(DEFAULT_QUESTIONS)


async def main() -> None:
    try:
        await seed()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
