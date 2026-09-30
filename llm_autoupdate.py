from __future__ import annotations

import asyncio
import json

from sqlalchemy.ext.asyncio import AsyncSession

from src.hls_platform.utils import initialize_llm
from src.db_session import AsyncSessionLocal


class LLMAutoUpdateAgent:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def generate_updated_html_sentence(
    self,
    old_claim: str,
    annotation_text: str,
    html_text: str,
    html_code: str,
    new_claim: str,
    document_type: str = "html",
    ) -> dict:

        prompt = f"""
    You are an expert pharmaceutical content editor.

    OLD CLAIM:
    {old_claim}

    NEW CLAIM:
    {new_claim}

    ANNOTATION TEXT:
    {annotation_text}

    HTML TEXT:
    {html_text}

    HTML CODE:
    {html_code}

    TASK:

1. Update the HTML CODE so that it reflects the NEW CLAIM.
2. Update the ANNOTATION TEXT so that it reflects the NEW CLAIM.

IMPORTANT RULES:

1. Preserve all HTML tags.
2. Preserve all styles.
3. Preserve all attributes.
4. Preserve all formatting.
5. Change only the content affected by the claim update.
6. Do not modify unrelated content.
7. Return valid HTML.
8. Update the annotation text using the same changes made in the HTML.
9. Do not add explanations.
10. Return ONLY valid JSON.
PDF-SPECIFIC RULES (apply only when the input content is from a PDF):
11. Preserve the existing sentence structure whenever possible.
12. Modify only the content required by the claim update.
13. Do not introduce additional clinical statements.
14. Do not expand the sentence unnecessarily.
15. Keep the updated annotation text the same length or shorter than the original annotation whenever possible.
16. The updated annotation should fit within the same document space as the original text.
17. Treat the provided PDF TEXT as the source paragraph.
18. The PDF TEXT already contains the correct paragraph structure.
19. Do NOT rewrite the paragraph.
20. Do NOT summarize the paragraph.
21. Do NOT generate a new sentence.
22. Find the content corresponding to the OLD CLAIM inside the PDF TEXT.
23. Replace only the OLD CLAIM content with the NEW CLAIM content.
24. Preserve all other words exactly as written.
25. The output should look like the original paragraph with only the claim-related words changed.
26. Return the FULL updated paragraph.
27. If you cannot clearly identify the claim portion, make the smallest possible modification.
Return JSON in this exact format:

{{
  "updated_annotation_text": "...",
  "updated_html_code": "..."
}}

    """

        llm = await initialize_llm(self.db)

        response = llm.invoke(prompt)

        response_text = getattr(
            response,
            "content",
            str(response)
        ).strip()

        import json


        response_text = response_text.strip()

        if response_text.startswith("```json"):
            response_text = response_text.replace("```json", "", 1)

        elif response_text.startswith("```"): 
            response_text = response_text.replace("```", "", 1)

        if response_text.endswith("```"):
            response_text = response_text[:-3]

        response_text = response_text.strip()

        try:
            result = json.loads(response_text)
        except Exception:
            print("❌ Failed to parse LLM JSON response")
            print(response_text)
            raise

        updated_annotation_text = result.get(
            "updated_annotation_text",
            annotation_text
        )

        updated_html_code = result.get(
            "updated_html_code",
            html_code
        )

        print("\n================ OLD CLAIM ================\n")
        print(old_claim)

        print("\n================ NEW CLAIM ================\n")
        print(new_claim)

        print("\n================ ANNOTATION TEXT ================\n")
        print(annotation_text)

        print("\n================ HTML TEXT ================\n")
        print(html_text)

        print("\n================ ORIGINAL HTML CODE ================\n")
        print(html_code)

        print("\n================ UPDATED HTML CODE ================\n")
        print(updated_html_code)

        print("\n================ UPDATED ANNOTATION TEXT ================\n")
        print(updated_annotation_text)

        return {
            "updated_annotation_text": updated_annotation_text,
            "updated_html_text": html_text,
            "updated_html_code": updated_html_code,
        }


    async def test_claim_update(self) -> str:

        old_claim = (
            "Synapta demonstrated a 34% increase in synaptic density over 12 months."
        )

        annotation_text = (
            "increase in synaptic density of 34% over 12 months alongside"
        )

        matched_html_paragraph = (
            "Clinical development data for Synapta include a Phase III result "
            "demonstrating an increase in synaptic density of 34% over 12 months "
            "alongside slowing of cognitive decline."
        )

        new_claim = (
            "Synapta demonstrated a 42% increase in synaptic density over 12 months."
        )

        return await self.generate_updated_html_sentence(
            old_claim=old_claim,
            annotation_text=annotation_text,
            matched_html_paragraph=matched_html_paragraph,
            new_claim=new_claim,
        )


async def main():

    async with AsyncSessionLocal() as db:

        agent = LLMAutoUpdateAgent(db)

        result = await agent.test_claim_update()

        print("\n================ FINAL RESULT ================\n")
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
