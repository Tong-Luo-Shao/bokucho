// The whole of `text` as pieces in order, the ones inside `marks` flagged. `marks` are
// [start, end) pairs counted in code points, as Python counts them, so a character outside
// the Basic Multilingual Plane counts once here although it takes two UTF-16 units.
export function markedPieces(text, marks) {
  const points = Array.from(text);
  const pieces = [];
  let cursor = 0;
  const push = (end, marked) => {
    if (end > cursor) pieces.push({ text: points.slice(cursor, end).join(""), marked });
    cursor = Math.max(cursor, end);
  };
  for (const [start, end] of marks) {
    push(start, false);
    push(end, true);
  }
  push(points.length, false);
  return pieces;
}

// A row of the preview is a few lines tall, and a mark far into a long line would fall
// outside it. A long run-up to the first mark is cut down to its last `lead` code points.
export function fromFirstMark(pieces, lead = 16) {
  const [first, ...rest] = pieces;
  if (!first || first.marked || rest.length === 0) return pieces;
  const points = Array.from(first.text);
  if (points.length <= lead) return pieces;
  return [{ text: `…${points.slice(-lead).join("")}`, marked: false }, ...rest];
}
