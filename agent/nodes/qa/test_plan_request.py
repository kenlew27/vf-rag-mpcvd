import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from agent.nodes.plan_request import (
    _clarification_reason,
    _database_handoff_issues,
    _matched_columns,
    database_vocabulary_context,
    plan_request,
)
from agent.request_plan import (
    KnowledgeSource,
    RequestPlan,
    RequestStatus,
    RequestTask,
    database_only_question,
    database_reference_question,
    request_plan_output_schema,
)
from agent.state import AgentState, UserQuery
from agent.supervisor_routing import routed_agents


class _Response:
    def __init__(self, payload):
        self.content = [{"text": json.dumps(payload)}]


class _Messages:
    def __init__(self, payload):
        self.payloads = payload if isinstance(payload, list) else [payload]
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Response(self.payloads.pop(0))


class _Client:
    def __init__(self, payload):
        self.messages = _Messages(payload)


class PlanRequestTests(unittest.TestCase):
    def plan(self, payload):
        return plan_request(
            AgentState(query=UserQuery(raw_text="Compare samples with papers.")),
            client=_Client(payload),
            model="claude-test",
        )

    def test_lookup_task_value(self):
        self.assertEqual(RequestTask.LOOKUP.value, "lookup")

    def test_ready_accepts_lookup(self):
        result = self.plan(
            {
                "tasks": ["lookup"],
                "knowledge_sources": ["structured_data", "external_literature"],
                "status": "ready",
                "reasons": [],
                "structured_data_question": "Compare the requested samples.",
            }
        )
        assert result.request_plan is not None
        self.assertEqual(result.request_plan.tasks, [RequestTask.LOOKUP])
        self.assertEqual(
            result.request_plan.knowledge_sources,
            [KnowledgeSource.STRUCTURED_DATA, KnowledgeSource.EXTERNAL_LITERATURE],
        )
        self.assertEqual(result.document_scopes, ["external"])
        self.assertEqual(result.request_plan.structured_data_question, "Compare the requested samples.")

    def test_compound_request_can_carry_a_database_only_question(self):
        plan = RequestPlan(
            tasks=[RequestTask.LOOKUP, RequestTask.GENERATE_HYPOTHESIS],
            knowledge_sources=[KnowledgeSource.STRUCTURED_DATA, KnowledgeSource.EXTERNAL_LITERATURE],
            status=RequestStatus.READY,
            structured_data_question=(
                "Calculate average growth rate and average birefringence for samples "
                "with growth temperature at least 900 C."
            ),
        )

        self.assertNotIn("literature", plan.structured_data_question.lower())

    def test_named_sample_metric_question_stays_narrow(self):
        plan = RequestPlan(
            tasks=[RequestTask.LOOKUP],
            knowledge_sources=[KnowledgeSource.STRUCTURED_DATA],
            status=RequestStatus.READY,
            structured_data_question="Retrieve growth rate for SAMPLE_1372.",
        )

        self.assertEqual(
            plan.structured_data_question,
            "Retrieve growth rate for SAMPLE_1372.",
        )

    def test_compound_sample_metric_question_uses_narrow_database_subquestion(self):
        question = (
            "For SAMPLE_X083, is the H2N2 setting consistent with the trends in research "
            "on nitrogen addition during high-rate homoepitaxial diamond growth?"
        )
        client = _Client({
            "tasks": ["lookup"],
            "knowledge_sources": ["structured_data", "external_literature"],
            "status": "ready",
            "reasons": [],
            "structured_data_question": "Retrieve the H2N2 setting for SAMPLE_X083.",
        })

        result = plan_request(
            AgentState(query=UserQuery(raw_text=question)),
            client=client,
            model="claude-test",
        )

        self.assertEqual(
            result.request_plan.structured_data_question,
            "Retrieve the H2N2 setting for SAMPLE_X083.",
        )
        self.assertEqual(client.messages.calls[0]["messages"][0]["content"], question)

    def test_explicit_reactors_cannot_be_rewritten_as_samples(self):
        question = "For reactors REACTOR_01 and REACTOR_02, compare pressure, growth rate, and quality outcomes."
        result = plan_request(
            AgentState(query=UserQuery(raw_text=question)),
            client=_Client({
                "tasks": ["lookup", "compare"],
                "knowledge_sources": ["structured_data"],
                "status": "ready",
                "reasons": [],
                "structured_data_question": "Retrieve pressure, growth rate, and quality outcomes for samples REACTOR_01 and REACTOR_02.",
            }),
            model="claude-test",
        )

        assert result.request_plan is not None
        self.assertEqual(
            result.request_plan.structured_data_question,
            "Retrieve pressure, growth rate, and quality outcomes for reactors REACTOR_01 and REACTOR_02.",
        )

    def test_compound_request_missing_database_question_clarifies_after_one_retry(self):
        client = _Client([
            {
                "tasks": ["lookup"],
                "knowledge_sources": ["structured_data", "external_literature"],
                "status": "ready",
                "reasons": [],
                "structured_data_question": None,
            },
            {
                "tasks": ["lookup"],
                "knowledge_sources": ["structured_data", "external_literature"],
                "status": "ready",
                "reasons": [],
                "structured_data_question": None,
            },
        ])
        result = plan_request(
            AgentState(query=UserQuery(raw_text="Compare samples with papers.")),
            client=client,
            model="claude-test",
        )

        assert result.request_plan is not None
        self.assertEqual(result.request_plan.status, RequestStatus.NEEDS_CLARIFICATION)
        self.assertEqual(len(client.messages.calls), 2)

    def test_model_output_schema_always_requires_nullable_database_question(self):
        self.assertIn("structured_data_question", request_plan_output_schema()["required"])

    def test_source_restriction_does_not_override_classifier_output(self):
        result = plan_request(
            AgentState(query=UserQuery(raw_text=(
                "Give me everything known about sample SAMPLE_1372. I’ll compare the result myself "
                "later with `Man-Made_Diamonds_1955.pdf`; only use the run data for this answer."
            ))),
            client=_Client({
                "tasks": ["lookup"],
                "knowledge_sources": ["structured_data", "internal_documents"],
                "status": "ready",
                "reasons": [],
                "structured_data_question": (
                    "Give me everything known about sample SAMPLE_1372 and compare it later with "
                    "Man-Made_Diamonds_1955.pdf."
                ),
            }),
            model="claude-test",
        )
        assert result.request_plan is not None
        self.assertEqual(
            result.request_plan.knowledge_sources,
            [KnowledgeSource.STRUCTURED_DATA, KnowledgeSource.INTERNAL_DOCUMENTS],
        )
        self.assertEqual(result.document_scopes, ["internal"])
        self.assertEqual(
            routed_agents(result.request_plan),
            ["table_agent", "internal_document_agent"],
        )
        self.assertEqual(
            result.request_plan.structured_data_question,
            "Give me everything known about sample SAMPLE_1372.",
        )

    def test_APP_identifier_does_not_override_classifier_output(self):
        result = plan_request(
            AgentState(query=UserQuery(raw_text=(
                "How do the conditions for SAMPLE_X096 compare with high-rate CVD conditions?"
            ))),
            client=_Client({
                "tasks": ["lookup", "compare"],
                "knowledge_sources": ["external_literature"],
                "status": "ready",
                "reasons": [],
                "structured_data_question": None,
            }),
            model="claude-test",
        )

        assert result.request_plan is not None
        self.assertEqual(
            result.request_plan.knowledge_sources,
            [KnowledgeSource.EXTERNAL_LITERATURE],
        )
        self.assertIsNone(result.request_plan.structured_data_question)
        self.assertEqual(result.document_scopes, ["external"])

    def test_general_knowledge_classifier_output_is_not_augmented(self):
        result = plan_request(
            AgentState(query=UserQuery(raw_text=(
                "How are substrate miscut, step flow, and surface morphology related "
                "in single-crystal diamond growth?"
            ))),
            client=_Client({
                "tasks": ["lookup"],
                "knowledge_sources": ["general_knowledge"],
                "status": "ready",
                "reasons": [],
                "structured_data_question": None,
            }),
            model="claude-test",
        )

        assert result.request_plan is not None
        self.assertEqual(
            result.request_plan.knowledge_sources,
            [KnowledgeSource.GENERAL_KNOWLEDGE],
        )
        self.assertEqual(result.document_scopes, [])

    def test_rejects_retired_answer_task(self):
        with self.assertRaises(ValidationError):
            RequestPlan(
                tasks=["answer"],
                knowledge_sources=["general_knowledge"],
                status=RequestStatus.READY,
            )

    def test_normalizes_task_order_and_deduplicates_sources(self):
        result = self.plan(
            {
                "tasks": ["generate_hypothesis", "compare", "lookup", "summarize", "compare"],
                "knowledge_sources": ["external_literature", "structured_data", "external_literature"],
                "status": "ready",
                "reasons": [],
                "structured_data_question": "Retrieve database evidence for the requested samples.",
            }
        )
        assert result.request_plan is not None
        self.assertEqual(
            result.request_plan.tasks,
            [
                RequestTask.LOOKUP,
                RequestTask.SUMMARIZE,
                RequestTask.COMPARE,
                RequestTask.GENERATE_HYPOTHESIS,
            ],
        )
        self.assertEqual(
            result.request_plan.knowledge_sources,
            [KnowledgeSource.EXTERNAL_LITERATURE, KnowledgeSource.STRUCTURED_DATA],
        )
        self.assertEqual(result.document_scopes, ["external"])

    def test_ready_requires_task_and_source(self):
        with self.assertRaises(ValidationError):
            RequestPlan(status=RequestStatus.READY)

    def test_non_ready_requires_reason(self):
        with self.assertRaises(ValidationError):
            RequestPlan(status=RequestStatus.NEEDS_CLARIFICATION)

    def test_clarification_and_unsupported_allow_empty_plan(self):
        clarification = RequestPlan(
            status=RequestStatus.NEEDS_CLARIFICATION,
            reasons=["No document target was identified."],
        )
        unsupported = RequestPlan(
            status=RequestStatus.UNSUPPORTED_REQUEST,
            reasons=["Flight booking is outside the materials workflow."],
        )
        self.assertEqual(clarification.tasks, [])
        self.assertEqual(unsupported.knowledge_sources, [])

    def test_planner_prompt_examples_match_contract(self):
        prompt = Path(__file__).parents[2] / "prompts" / "plan_request.md"
        examples = [
            ("What is the scientific name for diamond?", ["lookup"], ["general_knowledge"], "ready", []),
            ("Explain why methane affects diamond growth.", ["lookup"], ["external_literature", "general_knowledge"], "ready", []),
            ("What does Smith et al. 2024 report about binder chemistry?", ["lookup"], ["external_literature"], "ready", []),
            ("What papers discuss binder chemistry in this process?", ["lookup"], ["external_literature"], "ready", []),
            ("Summarize Smith et al. 2024.", ["lookup", "summarize"], ["external_literature"], "ready", []),
            ("Summarize the passage included in this request.", ["summarize"], ["internal_documents"], "ready", []),
            ("What growth rates were measured in runs using recipe A?", ["lookup"], ["structured_data"], "ready", []),
            ("Compare our measured samples with papers about binder chemistry.", ["lookup", "compare"], ["structured_data", "external_literature"], "ready", []),
            ("Find relevant papers, summarize their findings, and compare their conclusions.", ["lookup", "summarize", "compare"], ["external_literature"], "ready", []),
            ("Use our runs and relevant papers to propose a testable explanation.", ["lookup", "generate_hypothesis"], ["structured_data", "external_literature"], "ready", []),
            ("Summarize it.", [], [], "needs_clarification", ["No source material or identifiable target was provided."]),
        ]
        text = prompt.read_text()
        for user, tasks, sources, status, reasons in examples:
            payload = json.dumps(
                {
                    "tasks": tasks,
                    "knowledge_sources": sources,
                    "status": status,
                    "reasons": reasons,
                    "structured_data_question": None,
                },
                separators=(",", ":"),
            )
            if "structured_data" not in sources:
                self.assertIn(f'User: "{user}"\n```json\n{payload}', text)
        self.assertNotIn("Retrieve database evidence relevant to this request.", text)

    def test_planner_prompt_requires_narrow_sample_metric_question(self):
        text = (Path(__file__).parents[2] / "prompts" / "plan_request.md").read_text()
        self.assertIn("narrow metric lookup", text)
        self.assertIn("Retrieve <named metric or metrics> for <sample identifier>.", text)
        self.assertIn("reactors REACTOR_01 and REACTOR_02", text)

    def test_compact_vocabulary_contains_aliases_but_not_human_reference_material(self):
        vocabulary = database_vocabulary_context()

        self.assertIn('"growth temperature"', vocabulary)
        self.assertIn('"growth_temp"', vocabulary)
        self.assertIn('"process conditions"', vocabulary)
        self.assertNotIn("Optimization", vocabulary)
        self.assertLess(len(vocabulary), 4500)

    def test_holdout_style_handoffs_pass_without_false_retries(self):
        cases = [
            (
                "For each project, return its record count and average growth rate.",
                "Summarize records by project and compute row count and average growth_rate_µm_hr.",
            ),
            (
                "Return short runs at or below 31 hours and long runs at or above 49 hours "
                "with thickness and rate context.",
                "Return two nonoverlapping growth_duration_hours cohorts: at or below 31 "
                "and at or above 49.",
            ),
            (
                "For SAMPLE_X083 step 3, return start temperature, end temperature, and duration.",
                "Return step_3_GrowthStartTemp, step_3_GrowthEndTemp, and step_3_GrowthDur "
                "for SAMPLE_X083.",
            ),
        ]

        for question, handoff in cases:
            self.assertEqual(_database_handoff_issues(question, handoff), [])

    def test_mixed_source_reference_excludes_literature_only_literals(self):
        question = (
            "Return alpha_metric for category records. Compare the result with a "
            "915 MHz literature paper and explain any disagreement."
        )
        reference = database_reference_question(question)

        self.assertIn("alpha_metric", reference)
        self.assertNotIn("915", reference)
        self.assertEqual(_database_handoff_issues(reference, "Return alpha_metric for category records."), [])
        self.assertIn(
            "numeric value 915 was introduced without database reference",
            _database_handoff_issues(reference, "Return alpha_metric for category records at 915."),
        )

    def test_published_research_literals_are_excluded_from_database_reference(self):
        question = (
            "For project PROJ-A, use the database records to report each run’s chamber pressure, "
            "growth rate, morphology, and quality outcomes. Then compare the recorded chamber "
            "pressures with the 90–180 Torr range reported in published research on large-area "
            "diamond growth using a 915 MHz reactor. Use the 90–180 Torr range and 915 MHz "
            "frequency as literature context for the comparison rather than as conditions for "
            "selecting database records."
        )

        reference = database_reference_question(question)

        self.assertIn("project PROJ-A", reference)
        self.assertNotIn("90", reference)
        self.assertNotIn("180", reference)
        self.assertNotIn("915", reference)
        self.assertNotIn("Then", reference)
        self.assertEqual(
            _database_handoff_issues(
                reference,
                "For project PROJ-A, return growth_mean_pressure, growth_rate_µm_hr, morphology, and quality outcomes.",
            ),
            [],
        )

    def test_published_research_condition_is_excluded_from_database_reference(self):
        question = (
            "For sample SAMPLE_2338, use the database to report its recorded chamber pressure, "
            "microwave power, growth rate, morphology, birefringence, and available surface-quality "
            "indicators. Then compare those observations with published research describing "
            "homoepitaxial diamond growth at 300 Torr. Treat the 300 Torr value as the literature "
            "condition used for comparison, not as a requirement for the database record."
        )

        reference = database_reference_question(question)

        self.assertIn("SAMPLE_2338", reference)
        self.assertNotIn("300", reference)
        self.assertNotIn("Then", reference)

    def test_temperature_related_anomalies_map_to_the_flag_not_raw_temperature(self):
        columns = _matched_columns("runs flagged for either gas-related or temperature-related anomalies")

        self.assertEqual(columns, ["gas_flagged", "Temp_flag"])

    def test_reflected_microwave_power_maps_to_reflected_column_only(self):
        columns = _matched_columns("highest recorded reflected microwave power")

        self.assertEqual(columns, ["growth_mean_reflected"])

    def test_repeated_output_columns_do_not_block_an_executable_handoff(self):
        question = (
            "For reactor REACTOR_XX, compare runs whose recorded chamber pressure falls between 180 and 300 Torr "
            "with the other REACTOR_XX runs. Report differences in process variation, growth rate, morphology, "
            "and available quality indicators."
        )
        handoff = (
            "For reactor REACTOR_XX, compare runs with growth_mean_pressure between 180 and 300 to other REACTOR_XX runs, "
            "returning growth_mean_pressure, growth_mean_pressure, growth_rate_µm_hr, morphology, and quality indicators."
        )

        self.assertNotIn(
            "database output field 'growth_mean_pressure' was repeated",
            _database_handoff_issues(question, handoff),
        )

    def test_same_sentence_literature_only_number_never_reaches_database_handoff(self):
        question = "Return alpha_metric for category records and compare it with a 915 MHz paper."
        planned = "Compare the alpha_metric result with the 915 MHz paper."

        handoff = database_only_question(question, planned)

        self.assertIn("alpha_metric", handoff)
        self.assertNotIn("915", handoff)
        self.assertNotIn("paper", handoff.casefold())

    def test_controlled_clarification_reason_is_precise_and_bounded(self):
        reason = _clarification_reason(["unresolved handoff issue " * 30])

        self.assertTrue(reason.startswith("Database handoff could not be made executable:"))
        self.assertLessEqual(len(reason), 300)

    def test_process_identifier_cannot_be_rewritten_as_a_sample(self):
        issues = _database_handoff_issues(
            "Compare processes with process identifiers G1_170 and AB05_1498.",
            "Return samples G1_170 and AB05_1498 side-by-side.",
        )

        self.assertIn("explicit entity noun 'process' was not preserved", issues)

    def test_broad_field_bundles_require_physical_column_expansion(self):
        question = (
            "Review records for holder H_96_n3 and report the process conditions "
            "and final-quality results."
        )
        issues = _database_handoff_issues(
            question,
            "Return process conditions and final-quality results for holder H_96_n3.",
        )

        self.assertIn(
            "database field bundle must be expanded to its listed physical columns",
            issues,
        )
        aliases = {
            item["phrase"]: item["columns"]
            for item in json.loads(database_vocabulary_context())["aliases"]
        }
        fields = dict.fromkeys(
            aliases["process conditions"] + aliases["final-quality results"]
        )
        expanded = f"Return {', '.join(fields)} for holder H_96_n3."
        self.assertEqual(_database_handoff_issues(question, expanded), [])

    def test_lossy_handoff_retries_once_and_preserves_database_intent(self):
        question = (
            "For reactors REACTOR_XX and REACTOR_XX, return growth rate at or above 12, "
            "ordered side-by-side, and note missing fields."
        )
        client = _Client([
            {
                "tasks": ["lookup", "compare"],
                "knowledge_sources": ["structured_data"],
                "status": "ready",
                "reasons": [],
                "structured_data_question": "Return relevant database evidence and note missing fields.",
            },
            {
                "tasks": ["lookup"],
                "knowledge_sources": ["external_literature"],
                "status": "ready",
                "reasons": [],
                "structured_data_question": (
                    "Return growth_rate_µm_hr at or above 12 for reactors REACTOR_XX and REACTOR_XX "
                    "in an ordered side-by-side table."
                ),
            },
        ])

        result = plan_request(
            AgentState(query=UserQuery(raw_text=question)), client=client, model="claude-test"
        )

        assert result.request_plan is not None
        self.assertEqual(len(client.messages.calls), 2)
        self.assertEqual(
            result.request_plan.structured_data_question,
            "Return growth_rate_µm_hr at or above 12 for reactors REACTOR_XX and REACTOR_XX in an ordered side-by-side table.",
        )
        self.assertEqual(result.request_plan.tasks, [RequestTask.LOOKUP, RequestTask.COMPARE])
        self.assertEqual(result.request_plan.knowledge_sources, [KnowledgeSource.STRUCTURED_DATA])
        self.assertIn("Compact database vocabulary", client.messages.calls[0]["system"])
        self.assertIn("previous structured_data_question was rejected", client.messages.calls[1]["messages"][0]["content"])

    def test_invalid_retry_becomes_controlled_clarification(self):
        payload = {
            "tasks": ["lookup"],
            "knowledge_sources": ["structured_data"],
            "status": "ready",
            "reasons": [],
            "structured_data_question": "Return relevant database evidence.",
        }
        client = _Client([payload, payload])
        result = plan_request(
            AgentState(query=UserQuery(raw_text="Return growth rate for SAMPLE_1372.")),
            client=client,
            model="claude-test",
        )

        assert result.request_plan is not None
        self.assertEqual(result.request_plan.status, RequestStatus.NEEDS_CLARIFICATION)
        self.assertEqual(len(client.messages.calls), 2)
