/*:
 * @target MZ
 * @plugindesc Bokucho translation for RPG Maker MV/MZ. Load after other plugins.
 * @help Translates matching database text in memory. Original data stays on disk.
 */
(function () {
    "use strict";
    var translations = __BOKUCHO_TRANSLATIONS__;
    var pictures = __BOKUCHO_PICTURES__;
    var sources = Object.create(null);
    var owns = Object.prototype.hasOwnProperty;
    var loadDataFile = DataManager.loadDataFile;
    DataManager.loadDataFile = function (name, src) {
        sources[name] = src;
        return loadDataFile.apply(this, arguments);
    };
    var onLoad = DataManager.onLoad;
    DataManager.onLoad = function (object) {
        Object.keys(sources).forEach(function (name) {
            if (window[name] !== object) return;
            (translations[sources[name]] || []).forEach(function (group) {
                var cells = [];
                var matches = group.every(function (change) {
                    var parent = object;
                    for (var i = 0; i < change.path.length - 1; i++) {
                        var key = change.path[i];
                        if (!parent || !owns.call(parent, key)) return false;
                        parent = parent[key];
                    }
                    var last = change.path[change.path.length - 1];
                    if (!parent || !owns.call(parent, last) || parent[last] !== change.source) {
                        return false;
                    }
                    cells.push([parent, last, change.target]);
                    return true;
                });
                if (matches) {
                    cells.forEach(function (cell) { cell[0][cell[1]] = cell[2]; });
                }
            });
        });
        return onLoad.apply(this, arguments);
    };
    if (Object.keys(pictures).length) {
        // MV leaves Window.png unencrypted; its exception uses the original path.
        if (typeof Decrypter !== "undefined" && typeof Decrypter.checkImgIgnore === "function") {
            var checkImgIgnore = Decrypter.checkImgIgnore;
            Decrypter.checkImgIgnore = function (url) {
                return checkImgIgnore.call(this, url.indexOf("bokucho/") === 0 ? url.slice("bokucho/".length) : url);
            };
        }
        var loadBitmap = ImageManager.loadBitmap;
        ImageManager.loadBitmap = function (folder, filename) {
            if (pictures[folder + filename]) {
                var args = Array.prototype.slice.call(arguments);
                args[0] = "bokucho/" + folder;
                return loadBitmap.apply(this, args);
            }
            return loadBitmap.apply(this, arguments);
        };
    }
}());
