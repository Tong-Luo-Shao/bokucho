// qa_messages.js -- the sentences quality findings are told in. The backend
// names each finding by a code and hands over the values; the wording lives
// here so both interface languages have it. Each template must stay identical
// to bokucho.findings.TEMPLATES, the English the CLI prints.

export const QA_MESSAGES = {
  variables_missing: "missing variables: {missing}",
  variables_unexpected: "unexpected variables: {unexpected}",
  variables_both: "missing variables: {missing}; unexpected variables: {unexpected}",
  echo: "target is the source, handed back untranslated",
  language: "not written in {language}: {found}",
  dropped: "words dropped from: {lines}",
  length: "line of {longest} chars exceeds max_length {limit}",
  hans_residue: "still converts: {gist}",
  line_count: "line count changed: {source} -> {target}",
  brackets_speech_to_thought: "speech 「」 turned into thought （）: {source} -> {target}",
  brackets_thought_to_speech: "thought （） turned into speech 「」: {source} -> {target}",
  brackets_mixed_swap: "bracket kinds changed: {source} -> {target}",
  brackets_missing_or_extra: "brackets lost or added: {source} -> {target}",
  brackets_swapped: "reads as the translation of a neighbouring line: {source} -> {target}",
  brackets_speech_to_thought_fix: "speech 「」 turned into thought （）: {source} -> {target}; fix: {fixed}",
  brackets_thought_to_speech_fix: "thought （） turned into speech 「」: {source} -> {target}; fix: {fixed}",
  brackets_mixed_swap_fix: "bracket kinds changed: {source} -> {target}; fix: {fixed}",
  brackets_missing_or_extra_fix: "brackets lost or added: {source} -> {target}; fix: {fixed}",
  brackets_swapped_fix: "reads as the translation of a neighbouring line: {source} -> {target}; fix: {fixed}",
  values_search_any_found: "{where} searches {value} for any of {words}: found, but none of {forms} is in {form}",
  values_search_any_missing: "{where} searches {value} for any of {words}: not found, but one of {forms} is in {form}",
  values_search_found: "{where} searches {value} for {word}: found, but none of {forms} is in {form}",
  values_search_missing: "{where} searches {value} for {word}: not found, but one of {forms} is in {form}",
  values_cut: "{where} cuts {value} before {word} into {part}, but {form} into {got}",
  values_replace: "{where} replaces {old} with {new} in {value} to get {result}, but {form} gives {got}",
  values_join: "{where} joins {pieces} into {whole}, but the translations into {game}",
  script_key: "a script compares {source}: shipped here as {here}, as {elsewhere} at {where}",
  script_asset: "{source} names a game file: {file}",
};

export function checkLabel(t, check) {
  const labels = {
    variables: t("QaScreen", "Missing variables"),
    values: t("QaScreen", "Matched values"),
    echo: t("QaScreen", "Untranslated echo"),
    language: t("QaScreen", "Wrong language"),
    dropped: t("QaScreen", "Words dropped"),
    length: t("QaScreen", "Length"),
    "line-count": t("QaScreen", "Line count"),
    "hans-residue": t("QaScreen", "Simplified residue"),
    brackets: t("QaScreen", "Brackets"),
    script: t("QaScreen", "Script text"),
  };
  return labels[check] ?? check;
}

// A finding in the interface's language; one whose code this build does not
// know yet is shown in the backend's English rather than hidden.
export function issueText(t, issue) {
  if (!Object.hasOwn(QA_MESSAGES, issue.code)) return issue.message;
  const params = Object.fromEntries(
    Object.entries(issue.params ?? {}).map(([name, value]) => [
      name,
      value ?? t("QaIssue", "none"),
    ]),
  );
  return t("QaIssue", QA_MESSAGES[issue.code], params);
}
