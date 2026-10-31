"""Safe compiler diagnostics for bounded proposal repair, never source values."""


def compiled_contract_error(error):
    """Translate only known proposal-to-rule contract mismatches.

    Pydantic diagnostics may contain source input. Inspect the fixed validator
    code without formatting the exception, and let unexpected failures escape.
    """
    if error.title not in {'HierarchySpec','RuleSpec','ConceptMappingSpec','ConceptComponent'}:
        return None
    details=error.errors(include_url=False,include_input=False)
    if len(details)!=1 or details[0].get('type')!='value_error':
        return None
    cause=details[0].get('ctx',{}).get('error')
    arguments=getattr(cause,'args',())
    if len(arguments)==1 and isinstance(arguments[0],str) and arguments[0] in {
            'invalid_hierarchy_context','empty_hierarchy_context',*CONCEPT_CONTRACT_CODES}:
        return arguments[0]
    return None

# Semantic judgments are deliberately absent: a rejected meaning is not a
# formatting error to keep asking away. These codes describe fixable contracts.
HIERARCHY_IDENTITY_REPAIR_CODES = frozenset({
    'hierarchy_identity_resolution_unsupported',
    'hierarchy_identity_resolution_conflict',
    'hierarchy_identity_resolution_required',
})

CONCEPT_CONTRACT_CODES = frozenset({
    'concept_scope_evidence_required','concept_scope_document_required',
    'concept_identity_document_required','concept_namespace_invalid',
    'concept_context_type_must_be_distinct','concept_mapping_contract_conflict',
    'concept_mapping_document_required','concept_binding_must_be_source_scoped',
    'duplicate_concept_relation','concept_identity_domain_evidence_required',
    'concept_identity_domain_required',
})
CONTRACT_CANDIDATE_CODES = CONCEPT_CONTRACT_CODES | {
    'identity_domain_evidence_unresolved','excluded_column_projected'}

PROPOSAL_REPAIR_GUIDANCE = {
    'object_model_mapping_conflict': 'The proposal uses a code column as its label despite an explicit object model with a distinct name column for the same key and namespace. Reuse the issued name mapping, or explicitly revise the candidate object model if that understanding is wrong. Existing drafts need their current-session replace_draft_id. This feedback is not semantic confirmation; preserve independent evidence and validation.',
    'draft_replacement_required': 'A draft for this group is already retained. Revise it with the current-session replace_draft_id returned by the tool instead of submitting conflicting sibling versions. Preserve identity, namespace, topology, exclusions and evidence.',
    'invalid_draft_replacement': 'Use an issued current-session draft ID from the same submission tool and stable group_id or plan_id. Earlier-run IDs cannot be reused. Anonymous reference or exclusion drafts cannot be replaced through this mechanism.',
    'draft_replacement_changes_obligations': 'Display repair cannot change the retained identity, namespace, topology, exclusions or semantic obligations, or remove evidence. Keep those constraints; use an independently reviewed structural revision or abstain if a display-only correction cannot express the intended change.',
    'hierarchy_position_semantics_missing': 'A relative_path hierarchy must explicitly provide a nonempty identity_domain, nonempty taxonomy and position_semantics (ordinal_slots or ordered_slots). Derive each interpretation from the goal and issued evidence; do not invent defaults, copy row values, or drop relative_path or its restrictions just to pass compilation. Preserve the original scope, conditions and evidence. If these meanings cannot be established, abstain. The complete repaired claim still requires independent semantic review, and FD success does not supply semantic confirmation.',
    'hierarchy_identity_resolution_unsupported': 'Wide hierarchy supports explicit source_scoped with legacy paths or verified_identity_domain with relative_path. shared_definition and unknown policies are not aliases for either. Preserve the original identity request and its evidence in hierarchy_identity_requests; resolve the meaning explicitly or abstain. Do not omit or null identity_resolution. The repaired full claim still requires independent semantic review.',
    'hierarchy_identity_resolution_conflict': 'Align the explicit identity_resolution, path_semantics and identity_scope using evidence. source_scoped requires legacy paths and cannot mean domain_global; relative_path requires verified_identity_domain. namespace_local limits namespace values within a verified domain and can share across sources; it is not source isolation. Preserve the original request, conditions and evidence; do not drop or null the policy to bypass the conflict. Repair the meaning explicitly or abstain; independent semantic review is still required.',
    'hierarchy_identity_resolution_required': 'A previous hierarchy identity request remains unresolved in hierarchy_identity_requests. State an explicit supported identity_resolution consistent with path_semantics and identity_scope, preserving the original request and evidence. Omitting or nulling the policy, or renaming the proposal, cannot resolve the conflict. Resolve the meaning explicitly or abstain; the repaired full claim still requires independent semantic review.',
    'unbound_attribute_property': 'Every entity_key property_names key must also appear in attributes.single_valued or attributes.multi_valued. Explicitly classify evidence-supported attributes or correct unsupported mappings; do not add columns to identity to bypass this contract. Preserve all exclusions and semantic evidence.',
    'unregistered_attribute_semantics': 'Use an issued built-in property or declare the custom property type and entity kinds in semantic_schema. Identity code columns use identity_code with in_identity=true.',
    'builtin_property_definition_forbidden': 'Reference built-in properties without redefining their contracts. Declare a new name only when it represents a different permitted business attribute.',
    'builtin_relation_definition_forbidden': 'Reuse the built-in relationship without redefining its endpoint or validation contract.',
    'unregistered_node_kind': 'Declare the proposed business entity type in semantic_schema, or reuse an issued type supported by the evidence.',
    'property_not_in_output_contract': 'Declare the custom property in semantic_schema before using it; do not include excluded or evidence-only values.',
    'property_kind_not_registered': 'Register the applicable business entity kinds before declaring their properties.',
    'property_entity_kind_mismatch': 'Check the property definition against this object type; do not silently broaden an existing definition.',
    'property_multiplicity_mismatch': 'Respect the declared single-valued or multi-valued contract and use the corresponding attribute mapping.',
    'unregistered_relation': 'Declare the custom relation and its endpoint kinds in semantic_schema, or use an issued relation.',
    'relation_kind_not_registered': 'Register the applicable endpoint entity kinds before declaring the relation.',
    'relation_endpoint_kind_mismatch': 'The selected relation does not accept these business object kinds. verified_reference is a physical Column-to-Column relation, not a generic business edge. Choose an allowed evidence-supported business relation or declare its actual endpoint kinds; do not override an existing relation contract.',
    'semantic_definition_conflict': 'Reuse the issued project definition. Do not change its scalar type, multiplicity or endpoint contract implicitly.',
    'semantic_type_conflicts_with_system_kind': 'Choose an evidence-supported business type that does not redefine a metadata or provenance type.',
    'ambiguous_primary_label': 'Choose one primary label; map alternate labels as aliases with explicit property names.',
    'ambiguous_primary_attribute': 'Map each output property once and separate distinct business meanings with declared property names.',
    'ambiguous_alias_property': 'Use alias or english_name only once each, or explicitly define another permitted multi-valued property.',
    'too_many_alias_columns': 'Use explicit attribute definitions for additional meanings instead of unnamed positional aliases.',
    'namespace_not_in_identity': 'Keep namespace columns in the identity key; a namespace must not be lost from object identity.',
    'entity_identity_missing': 'Identify the object key columns explicitly using identity_code and in_identity=true, or abstain if no supported object key can be established.',
    'business_code_mapping_conflict': 'business_code is generated from the actual identity key. Do not map a different source column over it; use a separately defined permitted property for a different code meaning.',
    'outside_extraction_goal': 'Respect the nonempty entity_kinds requested by the user. Use an allowed type only if its meaning fits; otherwise abstain and preserve the goal.',
    'outside_relation_goal': 'Respect the nonempty relation_kinds requested by the user. Do not change the goal or relabel an unsupported relationship.',
    'reference_hierarchy_level_required': 'Select the source_level or target_level from the issued hierarchy entity_bindings; a wide hierarchy has a separate endpoint at each level.',
    'reference_hierarchy_level_unknown': 'Choose a level actually listed in entity_bindings.',
    'reference_level_on_nonhierarchy': 'Omit the level selector for ordinary entity groups and long hierarchies.',
    'reference_arity_mismatch': 'Map each current-table endpoint column, in order, to every key column of the selected entity binding.',
    'invalid_hierarchy_context': 'context_qualifiers supports only condition, valid_period, period and hierarchy_version. Put source namespace columns in namespace_columns, not context_qualifiers. Preserve all actual conditions; if they cannot be represented, abstain rather than silently removing them. Never copy row values into rule constants.',
    'empty_hierarchy_context': 'Hierarchy context qualifier values must be nonempty. Use an empty context_qualifiers mapping only when no actual condition is required; otherwise provide an evidence-supported permitted condition or abstain. Do not invent a value to satisfy the contract.',
}
PROPOSAL_REPAIR_GUIDANCE.update({
    'identity_domain_evidence_unresolved': 'Use current-table applicable complete non-derived definition sections from reference_options.definition_sections. Identity-domain fields require doc_id@revision:section_id, not column/probe IDs or a document name. Preserve the exact claimed domain, kind and scope. A valid locator still requires independent semantic review.',
    'excluded_column_projected': 'The proposed identity or attribute mapping uses a column excluded by the goal or this batch. Preserve exclusions; remove the conflicting projection or leave the candidate unresolved. Do not silently withdraw an exclusion to force publication.',
    **{code:'Repair the concept mapping contract using issued columns and complete applicable definition sections. Keep context identity distinct from concept identity, preserve namespace permissions, and use distinguishable role relationships. Do not invent global scope or remove restrictions to pass validation.' for code in CONCEPT_CONTRACT_CODES},
})
REPAIRABLE_PROPOSAL_CODES = frozenset(PROPOSAL_REPAIR_GUIDANCE)

# review_semantics already retries these once against its frozen proposition.
# Exhaustion must remain retryable on a later run, not cached as discovery done.
SEMANTIC_REVIEW_PROTOCOL_CODES = frozenset({
    'semantic_review_protocol_invalid',
    'semantic_review_proposition_mismatch', 'semantic_review_reference_mismatch',
    'semantic_review_proposition_missing', 'semantic_review_unissued_evidence',
    'semantic_review_incomplete_context', 'llm_schema_invalid',
    'semantic_review_patch_invalid',
})
MAX_PROPOSAL_REPAIR_ROUNDS = 2
