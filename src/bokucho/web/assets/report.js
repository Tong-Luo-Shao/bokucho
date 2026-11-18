// report.js -- what the error banner hands to a bug report.

// The headline as the translator read it, then the raw details when there are any.
export function reportedError(headline, detail) {
  return detail ? `${headline}\n\n${detail}` : headline;
}

// Both Report buttons go through here: a report that cannot open says why.
export function reportProblem(bridge, error, showError) {
  return bridge.report_problem(error).catch(showError);
}
