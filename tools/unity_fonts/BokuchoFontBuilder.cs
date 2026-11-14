// Builds the TextMesh Pro fallback font that Bokucho's Unity patch hands to XUnity.AutoTranslator.
// Run by tools/build_unity_fonts.py inside a throwaway project, once per Unity generation:
//   Unity -batchmode -projectPath <project> -executeMethod BokuchoFontBuilder.Build
//         -bokuchoFont Assets/<face> -bokuchoChars <file> -bokuchoBundle <name> -bokuchoOut <folder>
// (legacy editors: no -batchmode, and -createProject in place of -projectPath)
using System;
using System.IO;
using TMPro;
using UnityEditor;
using UnityEngine;
using UnityEngine.TextCore.LowLevel;

public static class BokuchoFontBuilder
{
    const int AtlasSize = 8192;
    const int Padding = 5;
    // About 32,000 glyphs fill an 8192 atlas near 30pt; a larger try only wastes a full pack.
    static readonly int[] PointSizes = { 30, 28, 26, 24, 22, 20 };

    public static void Build()
    {
        try
        {
            string fontPath = Argument("-bokuchoFont");
            string characters = File.ReadAllText(Argument("-bokuchoChars"));
            string bundle = Argument("-bokuchoBundle");
            string output = Argument("-bokuchoOut");

            AssetDatabase.ImportAsset(fontPath);
            Font font = AssetDatabase.LoadAssetAtPath<Font>(fontPath);
            if (font == null) throw new Exception("Unity could not import " + fontPath);

            TMP_FontAsset asset = Populate(font, characters);
            string assetPath = "Assets/" + bundle + ".asset";
            AssetDatabase.CreateAsset(asset, assetPath);
            foreach (Texture2D atlas in asset.atlasTextures)
            {
                atlas.name = bundle + " Atlas";
                AssetDatabase.AddObjectToAsset(atlas, asset);
            }
            asset.material.name = bundle + " Material";
            AssetDatabase.AddObjectToAsset(asset.material, asset);

            // A static asset needs no face: drop it so the bundle carries only the atlas.
            asset.atlasPopulationMode = AtlasPopulationMode.Static;
            SerializedObject serialized = new SerializedObject(asset);
            foreach (string field in new[] { "m_SourceFontFile", "m_SourceFontFile_EditorRef" })
            {
                SerializedProperty property = serialized.FindProperty(field);
                if (property != null) property.objectReferenceValue = null;
            }
            serialized.ApplyModifiedPropertiesWithoutUndo();
            EditorUtility.SetDirty(asset);
            AssetDatabase.SaveAssets();

            Directory.CreateDirectory(output);
            AssetBundleBuild build = new AssetBundleBuild
            {
                assetBundleName = bundle,
                assetNames = new[] { assetPath },
            };
            BuildPipeline.BuildAssetBundles(
                output,
                new[] { build },
                BuildAssetBundleOptions.ChunkBasedCompression,
                BuildTarget.StandaloneWindows64);
            Debug.Log("Bokucho font built: " + asset.characterTable.Count + " characters at "
                      + asset.faceInfo.pointSize + "pt");
            EditorApplication.Exit(0);
        }
        catch (Exception exception)
        {
            Debug.LogError("Bokucho font build failed: " + exception);
            EditorApplication.Exit(1);
        }
    }

    // Leaves once a new project's packages are in: a legacy editor with a window resolves
    // them only on the way to -executeMethod, not with -quit.
    public static void Quit()
    {
        EditorApplication.Exit(0);
    }

    // The largest sampling size at which every character fits one atlas.
    static TMP_FontAsset Populate(Font font, string characters)
    {
        foreach (int pointSize in PointSizes)
        {
            TMP_FontAsset asset = Create(font, pointSize);
            // One line per call: the first brings every glyph, the second only shares them.
            string[] lines = characters.Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries);
            int missing = 0;
            foreach (string line in lines)
            {
                Add(asset, line);
                foreach (char character in line)
                {
                    if (!asset.characterLookupTable.ContainsKey(character)) missing++;
                }
            }
            if (missing == 0)
            {
                foreach (Texture2D atlas in asset.atlasTextures) atlas.Apply(false, false);
                return asset;
            }
            Debug.Log("Bokucho font: " + missing + " characters do not fit at " + pointSize + "pt");
            UnityEngine.Object.DestroyImmediate(asset);
        }
        throw new Exception("The characters do not fit one atlas even at the smallest size");
    }

    // TMP 1.4 and 2.0 read past the glyphs the engine packed and throw before listing the
    // characters. The packed glyphs stay, so another call lists them and packs only the rest.
    static void Add(TMP_FontAsset asset, string line)
    {
        int progress = -1;
        while (true)
        {
            try
            {
                string ignored;
                asset.TryAddCharacters(line, out ignored);
                return;
            }
            catch (NullReferenceException)
            {
                int now = asset.glyphTable.Count + asset.characterLookupTable.Count;
                if (now == progress) return;
                progress = now;
                Debug.Log("Bokucho font: TMP threw with " + asset.glyphTable.Count + " glyphs and "
                          + asset.characterLookupTable.Count + " characters in; adding again");
            }
        }
    }

    static TMP_FontAsset Create(Font font, int pointSize)
    {
#if UNITY_2020_2_OR_NEWER
        // One atlas, as on the older generations, so every bundle behaves the same.
        return TMP_FontAsset.CreateFontAsset(
            font, pointSize, Padding, GlyphRenderMode.SDFAA, AtlasSize, AtlasSize,
            AtlasPopulationMode.Dynamic, false);
#else
        return TMP_FontAsset.CreateFontAsset(
            font, pointSize, Padding, GlyphRenderMode.SDFAA, AtlasSize, AtlasSize,
            AtlasPopulationMode.Dynamic);
#endif
    }

    static string Argument(string name)
    {
        string[] arguments = Environment.GetCommandLineArgs();
        int index = Array.IndexOf(arguments, name);
        if (index < 0 || index + 1 >= arguments.Length) throw new Exception("Missing " + name);
        return arguments[index + 1];
    }
}
