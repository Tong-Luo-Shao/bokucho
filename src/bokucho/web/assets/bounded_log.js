const DEFAULT_LIMIT = 300;

export function createBoundedLog(limit = DEFAULT_LIMIT) {
  const retained = [];
  let dropped = 0;
  let liveKey = null;

  function push(line) {
    retained.push(line);
    const overflow = retained.length - limit;
    if (overflow > 0) {
      retained.splice(0, overflow);
      dropped += overflow;
    }
  }

  return {
    append(line) {
      if (!line) return;
      liveKey = null;
      push(line);
    },
    upsert(key, line) {
      if (!line) return;
      if (liveKey === key && retained.length > 0) {
        retained[retained.length - 1] = line;
      } else {
        push(line);
      }
      liveKey = key;
    },
    clear() {
      retained.length = 0;
      dropped = 0;
      liveKey = null;
    },
    lines() {
      return retained.slice();
    },
    get dropped() {
      return dropped;
    },
  };
}
