"""Safe compiler diagnostics for bounded proposal repair, never source values."""

# Semantic judgments are deliberately absent: a rejected meaning is not a
# formatting error to keep asking away. These codes describe fixable contracts.
PROPOSAL_REPAIR_GUIDANCE = {
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
}
REPAIRABLE_PROPOSAL_CODES = frozenset(PROPOSAL_REPAIR_GUIDANCE)

# review_semantics already retries these once against its frozen proposition.
# Exhaustion must remain retryable on a later run, not cached as discovery done.
SEMANTIC_REVIEW_PROTOCOL_CODES = frozenset({
    'semantic_review_proposition_mismatch', 'semantic_review_reference_mismatch',
    'semantic_review_proposition_missing', 'semantic_review_unissued_evidence',
    'semantic_review_incomplete_context', 'llm_schema_invalid',
})
MAX_PROPOSAL_REPAIR_ROUNDS = 2
