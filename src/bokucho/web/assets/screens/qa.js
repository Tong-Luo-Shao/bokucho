// qa.js -- quality checks as a rule rail: pick a check on the left,
// read its hits on the right, and jump to the affected text.
//
// There is nothing to start here. The checks read a unit's source and target,
// so the shell recomputes them after every edit and every job and this screen
// draws whatever the last reading said.

import { el, setChildren } from "../dom.js";
import { choiceDialog, confirmDialog } from "../confirm.js";
import { checkLabel, issueText } from "../qa_messages.js";

// The order the rail lists checks in; line-count differences are advisory.
const RULE_ORDER = [
  "variables",
  "values",
  "echo",
  "language",
  "dropped",
  "length",
  "line-count",
  "hans-residue",
  "brackets",
  "script",
];
const PAGE_SIZE = 100;
export function splitQaJoke(joke) {
  const questionIndex = [joke.indexOf("?"), joke.indexOf("？")]
    .filter((index) => index >= 0)
    .sort((left, right) => left - right)[0];
  if (questionIndex !== undefined) {
    let answerStart = questionIndex + 1;
    const closingPunctuation = new Set(["\"", "'", "”", "」", "』", ")", "）", "]", "}"]);
    while (closingPunctuation.has(joke[answerStart])) answerStart += 1;
    return {
      question: joke.slice(0, answerStart).trim(),
      answer: joke.slice(answerStart).trim(),
    };
  }

  for (const separator of [". ", "。", ", ", "，"]) {
    const separatorIndex = joke.indexOf(separator);
    const answerStart = separatorIndex + separator.length;
    if (separatorIndex >= 0 && joke.slice(answerStart).trim()) {
      return {
        question: joke.slice(0, separatorIndex + separator.trimEnd().length).trim(),
        answer: joke.slice(answerStart).trim(),
      };
    }
  }
  return { question: joke, answer: "" };
}

export function randomQaJoke(t, random = Math.random, current = null) {
  const jokes = [
    t("QaScreen", "Why did the scarecrow win an award? He was outstanding in his field."),
    t("QaScreen", "I only know 25 letters of the alphabet. I don't know y."),
    t("QaScreen", "What do you call fake spaghetti? An impasta."),
    t("QaScreen", "Why don't eggs tell jokes? They might crack each other up."),
    t("QaScreen", "What did one wall say to the other? I'll meet you at the corner."),
    t("QaScreen", "Why was the math book sad? It had too many problems."),
    t("QaScreen", "What do you call cheese that isn't yours? Nacho cheese."),
    t("QaScreen", "Why did the tomato blush? It saw the salad dressing."),
    t("QaScreen", "What do you call a bear with no teeth? A gummy bear."),
    t("QaScreen", "Why can't your nose be 12 inches long? Then it would be a foot."),
    t("QaScreen", "What kind of tree fits in your hand? A palm tree."),
    t("QaScreen", "What do clouds wear under their shorts? Thunderpants."),
    t("QaScreen", "Why did the coffee file a police report? It got mugged."),
    t("QaScreen", "What do you call a sleeping bull? A bulldozer."),
    t("QaScreen", "Why did the golfer bring two pairs of pants? In case he got a hole in one."),
    t("QaScreen", "What do you call a fish wearing a bow tie? Sofishticated."),
    t("QaScreen", "Why are elevator jokes so good? They work on many levels."),
    t("QaScreen", "What did the ocean say to the beach? Nothing, it just waved."),
    t("QaScreen", "Why did the computer go to the doctor? It had a virus."),
    t("QaScreen", "What do you call a factory that makes okay products? A satisfactory."),
    t("QaScreen", "Why did the cookie go to the hospital? It felt crummy."),
    t("QaScreen", "How does a penguin build its house? Igloos it together."),
    t("QaScreen", "Why did the student eat his homework? The teacher said it was a piece of cake."),
    t("QaScreen", "What do you call a belt made of watches? A waist of time."),
    t("QaScreen", "Why was the broom late? It swept in."),
    t("QaScreen", "What do you call an alligator in a vest? An investigator."),
    t("QaScreen", "Why did the banana go to the doctor? It wasn't peeling well."),
    t("QaScreen", "How do you organize a space party? You planet."),
    t("QaScreen", "What do you call a cow with no legs? Ground beef."),
    t("QaScreen", "Why did the picture go to jail? It was framed."),
    t("QaScreen", "What do you call a dinosaur with a great vocabulary? A thesaurus."),
    t("QaScreen", "Why don't skeletons fight each other? They don't have the guts."),
    t("QaScreen", "Why did the orange stop? It ran out of juice."),
    t("QaScreen", "What do you call a pile of cats? A meowtain."),
    t("QaScreen", "Why was six afraid of seven? Because seven eight nine."),
    t("QaScreen", "What kind of music do balloons hate? Pop."),
    t("QaScreen", "Why did the clock get hungry? It went back four seconds."),
    t("QaScreen", "What do you call a boomerang that won't come back? A stick."),
    t("QaScreen", "Why did the calendar get nervous? Its days were numbered."),
    t("QaScreen", "What do you call a dog that can do magic? A Labracadabrador."),
    t("QaScreen", "Why don't oysters donate to charity? Because they are shellfish."),
    t("QaScreen", "What did the zero say to the eight? Nice belt."),
    t("QaScreen", "Why did the stadium get hot? All the fans left."),
    t("QaScreen", "What do you call a can opener that doesn't work? A can't opener."),
    t("QaScreen", "Why was the computer cold? It left its Windows open."),
    t("QaScreen", "Why did the mushroom get invited to every party? He was a fungi."),
    t("QaScreen", "What did the left eye say to the right eye? Between us, something smells."),
    t("QaScreen", "Why did the bicycle fall over? It was two-tired."),
  ];
  const candidates = current
    ? jokes.filter((joke) => {
      const parts = splitQaJoke(joke);
      return parts.question !== current.question || parts.answer !== current.answer;
    })
    : jokes;
  return splitQaJoke(candidates[Math.floor(random() * candidates.length)]);
}

export function pageIssues(issues, page, pageSize = PAGE_SIZE) {
  const pageCount = Math.max(1, Math.ceil(issues.length / pageSize));
  const currentPage = Math.max(0, Math.min(page, pageCount - 1));
  const start = currentPage * pageSize;
  return {
    page: currentPage,
    pageCount,
    start,
    issues: issues.slice(start, start + pageSize),
  };
}

function byRuleOrder(checks) {
  return [...checks].sort((a, b) => RULE_ORDER.indexOf(a) - RULE_ORDER.indexOf(b));
}

// Each bulk fix is counted first, since it moves lines; one with nothing to do
// says so instead of asking.
async function previewThenStart(ctx, { preview, empty, nothingToDo, message, confirmText, job }) {
  const { t } = ctx;
  try {
    const counts = await preview();
    if (empty(counts)) {
      await choiceDialog({
        message: nothingToDo,
        choices: [{ key: true, label: t("Common", "Confirm"), primary: true }],
      });
      return;
    }
    const confirmed = await confirmDialog(ctx, { message: message(counts), confirmText });
    if (confirmed) await ctx.startJob(job);
  } catch (err) {
    ctx.showError(err);
  }
}

// A machine draft saved before a check existed is held to it here: what a
// translation run would put right is put right, and the rest goes back empty
// to be translated again.
function recheck(ctx) {
  const { t } = ctx;
  return previewThenStart(ctx, {
    preview: () => ctx.bridge.recheck_preview(),
    empty: ({ settled, sent_back: sentBack }) => settled === 0 && sentBack === 0,
    nothingToDo: t("QaScreen", "Every machine draft passes today's checks."),
    message: ({ settled, sent_back: sentBack }) => t(
      "QaScreen",
      "{settled} machine drafts will be cleaned up where they stand. {sent_back} will be emptied and sent back to be translated; until then the game shows their original lines.",
      { settled, sent_back: sentBack },
    ),
    confirmText: t("QaScreen", "Clean up and send back"),
    job: "recheck",
  });
}

// Only machine drafts are written; a line a person reviewed keeps its brackets.
function fixBrackets(ctx) {
  const { t } = ctx;
  return previewThenStart(ctx, {
    preview: () => ctx.bridge.brackets_preview(),
    empty: ({ fixed }) => fixed === 0,
    nothingToDo: t("QaScreen", "No machine draft has brackets a rule can put back."),
    message: ({ fixed }) => t(
      "QaScreen",
      "{fixed} machine-translated lines will get their brackets back as the original has them. Reviewed lines stay as they are.",
      { fixed },
    ),
    confirmText: t("QaScreen", "Fix brackets"),
    job: "fix-brackets",
  });
}

// A value the game takes apart or puts together is given its parts'
// translations; only machine drafts are written, as with brackets.
function fixValues(ctx) {
  const { t } = ctx;
  return previewThenStart(ctx, {
    preview: () => ctx.bridge.values_preview(),
    empty: ({ fixed }) => fixed === 0,
    nothingToDo: t("QaScreen", "No machine draft is a value its parts can put right."),
    message: ({ fixed }) => t(
      "QaScreen",
      "{fixed} machine-translated values will be put together from the translations of their parts, so the game's checks on them hold. Reviewed values stay as they are.",
      { fixed },
    ),
    confirmText: t("QaScreen", "Fix matched values"),
    job: "fix-values",
  });
}

export function mount(container, ctx) {
  const screen = new QaScreen(ctx);
  container.replaceChildren(screen.element);
  const unsubscribe = ctx.onQuality(() => screen.render());

  function refresh() {
    screen.render();
    // Mounting is the one moment the screen cannot assume a reading is current:
    // it may not have existed when the project was opened.
    ctx.refreshQuality();
  }

  refresh();

  return {
    refresh,
    unmount() {
      unsubscribe();
    },
  };
}

class QaScreen {
  constructor(ctx) {
    const { t } = ctx;
    this.ctx = ctx;
    this.emptyJoke = randomQaJoke(t);
    this.selected = null;
    this.page = 0;
    this.railList = el("div", { class: "qa-rail-list", role: "list" });
    this.railTotal = el("div", { class: "qa-rail-total" });
    this.mainTitle = el("span", { class: "qa-main-title" });
    this.mainCount = el("span", { class: "qa-main-count mono" });
    const fixButton = (text, onClick) =>
      el("button", { class: "btn btn-sm", type: "button", text, hidden: true, onClick });
    // Shown with the brackets rule: the fix it names is one a rule writes.
    this.fixBracketsButton = fixButton(t("QaScreen", "Fix brackets"), () => fixBrackets(ctx));
    this.fixValuesButton = fixButton(t("QaScreen", "Fix matched values"), () => fixValues(ctx));
    const pagerButton = (text, step) => el("button", {
      class: "qa-pager-button",
      type: "button",
      text,
      onClick: () => this.turnPage(step),
    });
    this.previousPage = pagerButton(t("QaScreen", "Previous"), -1);
    this.nextPage = pagerButton(t("QaScreen", "Next"), 1);
    this.pagerStatus = el("span", { class: "qa-pager-status mono" });
    this.pager = el("div", { class: "qa-pager", hidden: true }, [
      this.previousPage,
      this.pagerStatus,
      this.nextPage,
    ]);
    this.hits = el("div", { class: "qa-hits", role: "list" });
    this.element = el("div", { class: "qa-screen" }, [this.rail(), this.main()]);
  }

  rail() {
    const { t } = this.ctx;
    return el("nav", { class: "qa-rail", "aria-label": t("QaScreen", "Rules") }, [
      el("div", { class: "qa-rail-head", text: t("QaScreen", "Rules") }),
      this.railList,
      this.railTotal,
      el("button", {
        class: "btn btn-sm qa-recheck",
        type: "button",
        text: t("QaScreen", "Clean up and send back"),
        onClick: () => recheck(this.ctx),
      }),
    ]);
  }

  main() {
    return el("div", { class: "qa-main" }, [
      el("div", { class: "qa-main-head" }, [
        this.mainTitle,
        this.mainCount,
        el("span", { class: "spacer" }),
        this.fixBracketsButton,
        this.fixValuesButton,
      ]),
      this.pager,
      this.hits,
    ]);
  }

  report() {
    return this.ctx.appState.qaReport ?? null;
  }

  groups() {
    const map = new Map();
    for (const issue of this.report()?.issues ?? []) {
      if (!map.has(issue.check)) map.set(issue.check, []);
      map.get(issue.check).push(issue);
    }
    return map;
  }

  render() {
    const byCheck = this.groups();
    if (this.selected === null || !byCheck.has(this.selected)) {
      this.selected = byRuleOrder(byCheck.keys())[0] ?? null;
      this.page = 0;
    }
    this.renderRail(byCheck);
    this.renderHits(byCheck);
  }

  turnPage(step) {
    this.page += step;
    this.renderHits(this.groups());
  }

  renderRail(byCheck) {
    const { t } = this.ctx;
    setChildren(this.railList, byRuleOrder(byCheck.keys()).map((check) =>
      this.ruleButton(check, byCheck.get(check))));
    const issues = this.report()?.issues ?? [];
    const defects = issues.filter((issue) => issue.severity !== "warning").length;
    const warnings = issues.length - defects;
    this.railTotal.textContent = t("QaScreen", "{defects} defects · {warnings} warnings", {
      defects,
      warnings,
    });
  }

  ruleButton(check, issues) {
    const label = checkLabel(this.ctx.t, check);
    return el("button", {
      class: "qa-rule",
      type: "button",
      "data-selected": check === this.selected ? "true" : null,
      "data-severity": issues[0].severity ?? "error",
      onClick: () => {
        this.selected = check;
        this.page = 0;
        this.render();
      },
    }, [
      el("span", { class: "qa-rule-dot" }),
      el("span", { class: "qa-rule-label", text: label, title: label }),
      el("span", { class: "qa-rule-count mono", text: String(issues.length) }),
    ]);
  }

  renderHits(byCheck) {
    const { t } = this.ctx;
    const selected = this.selected;
    const issues = selected ? byCheck.get(selected) ?? [] : [];
    this.mainTitle.textContent = selected ? checkLabel(t, selected) : t("QaScreen", "Quality");
    this.mainCount.textContent = selected ? String(issues.length) : "";
    this.fixBracketsButton.hidden = selected !== "brackets";
    this.fixValuesButton.hidden = selected !== "values";
    if (!this.report()) {
      this.pager.hidden = true;
      setChildren(this.hits, el("p", { class: "qa-empty", text: t("QaScreen", "Nothing to check yet") }));
      return;
    }
    if (issues.length === 0) {
      this.pager.hidden = true;
      setChildren(this.hits, this.jokeState());
      return;
    }
    const pageResult = pageIssues(issues, this.page);
    this.page = pageResult.page;
    this.renderPager(pageResult, issues.length);
    setChildren(this.hits, pageResult.issues.map((issue, index) =>
      this.hitRow(issue, pageResult.start + index + 1)));
  }

  jokeState() {
    const { t } = this.ctx;
    const jokeQuestion = el("span", { class: "qa-joke-question", text: this.emptyJoke.question });
    const jokeAnswer = el("span", { class: "qa-joke-answer", text: this.emptyJoke.answer });
    const emptyState = el("div", { class: "qa-empty-state" }, [
      el("p", { class: "qa-empty", text: t("QaScreen", "No hits for this check") }),
      el("p", { class: "qa-joke", tabindex: "0" }, [jokeQuestion, jokeAnswer]),
    ]);
    emptyState.addEventListener("click", (event) => {
      if (event.target !== emptyState) return;
      this.emptyJoke = randomQaJoke(t, Math.random, this.emptyJoke);
      jokeQuestion.textContent = this.emptyJoke.question;
      jokeAnswer.textContent = this.emptyJoke.answer;
    });
    return emptyState;
  }

  renderPager(pageResult, total) {
    this.pager.hidden = pageResult.pageCount <= 1;
    this.previousPage.disabled = this.page === 0;
    this.nextPage.disabled = this.page >= pageResult.pageCount - 1;
    this.pagerStatus.textContent = this.ctx.t("QaScreen", "{start}–{end} of {total}", {
      start: pageResult.start + 1,
      end: pageResult.start + pageResult.issues.length,
      total,
    });
  }

  hitRow(issue, number) {
    const { t } = this.ctx;
    const message = issueText(t, issue);
    return el("div", {
      class: "qa-hit",
      role: "listitem",
      "data-severity": issue.severity ?? "error",
    }, [
      el("span", { class: "qa-hit-n mono", text: String(number) }),
      el("span", { class: "qa-hit-unit mono", text: issue.unit_id, title: issue.unit_id }),
      el("span", {
        class: "qa-hit-msg",
        text: issue.severity === "warning"
          ? t("QaScreen", "Warning: {message}", { message })
          : message,
        title: message,
      }),
      el("span", { class: "qa-hit-file", text: issue.file, title: issue.file }),
      el("button", {
        class: "qa-go",
        type: "button",
        text: t("QaScreen", "Go"),
        onClick: () => {
          this.ctx.appState.pendingQaTarget = { source_file: issue.file, id: issue.unit_id };
          this.ctx.navigate("strings");
        },
      }),
    ]);
  }
}
