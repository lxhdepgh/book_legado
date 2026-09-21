import io.legado.app.model.analyzeRule.AnalyzeByJSonPath
import io.legado.app.model.analyzeRule.AnalyzeByJSoup
import io.legado.app.model.analyzeRule.AnalyzeByRegex
import io.legado.app.model.analyzeRule.AnalyzeByXPath
import net.minidev.json.JSONObject
import net.minidev.json.JSONValue
import net.minidev.json.parser.JSONParser
import java.io.File

// Differential harness: feeds the SAME (body, rule) pairs into Legado's own
// Kotlin analyzers and the Python engine, so every disagreement is an engine
// difference rather than a network difference.

fun main(args: Array<String>) {
    val casesPath = args[0]
    val outPath = args[1]
    val parser = JSONParser(JSONParser.DEFAULT_PERMISSIVE_MODE)
    File(outPath).bufferedWriter(Charsets.UTF_8).use { writer ->
        File(casesPath).forEachLine(Charsets.UTF_8) { line ->
            if (line.isBlank()) return@forEachLine
            val case = parser.parse(line) as Map<*, *>
            val id = case["id"].toString()
            val engine = case["engine"].toString()
            val content = case["content"] as String
            var rule = case["rule"] as String
            val output = JSONObject()
            output["id"] = id
            try {
                // AnalyzeRule strips the @json: prefix before delegating.
                if (engine == "json" && rule.lowercase().startsWith("@json:"))
                    rule = rule.substringAfter(":").trim()
                val values: Any? = when (engine) {
                    // Legado's bookList path uses getElements (AnalyzeRule
                    // returns Elements for the list rule), not getStringList.
                    "jsoup" -> AnalyzeByJSoup(content).getElements(rule).map { it.text() }
                    // AnalyzeRule.getString / getString(isUrl=true) delegate to
                    // AnalyzeByJSoup.getString (join "\n") and getString0 (first
                    // item only).  They are separate code paths from getElements.
                    "jsoup_string" -> AnalyzeByJSoup(content).getString(rule) ?: ""
                    "jsoup_string0" -> AnalyzeByJSoup(content).getString0(rule)
                    "jsoup_string_list" ->
                        AnalyzeByJSoup(content).getStringList(rule)
                    "json" -> AnalyzeByJSonPath(content).getList(rule)
                    "xpath" -> AnalyzeByXPath(content).getStringList(rule)
                    "regex" -> AnalyzeByRegex.getElements(
                        content,
                        rule.split("&&").map { it.trim() }.toTypedArray())
                    else -> error("unknown engine: $engine")
                }
                output["ok"] = true
                output["values"] = values
            } catch (exc: Exception) {
                output["ok"] = false
                output["error"] = (exc.toString())
            }
            writer.write(JSONValue.toJSONString(output))
            writer.newLine()
        }
    }
}
