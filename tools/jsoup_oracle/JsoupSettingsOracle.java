import java.nio.charset.StandardCharsets;
import java.util.Base64;
import org.jsoup.Jsoup;
import org.jsoup.nodes.Document;
import org.jsoup.nodes.Entities;

/** Fixed Jsoup 1.16.2 OutputSettings oracle. */
public final class JsoupSettingsOracle {
    public static void main(String[] args) {
        Document document = Jsoup.parse(args[1]);
        Document.OutputSettings settings = document.outputSettings();
        switch (args[0]) {
            case "compact" -> settings.prettyPrint(false);
            case "indent2" -> settings.prettyPrint(true).indentAmount(2);
            case "xml" -> settings.prettyPrint(false)
                    .syntax(Document.OutputSettings.Syntax.xml)
                    .escapeMode(Entities.EscapeMode.xhtml);
            default -> throw new IllegalArgumentException(args[0]);
        }
        String value = document.selectFirst(args[2]).outerHtml();
        System.out.println(Base64.getEncoder().encodeToString(
                value.getBytes(StandardCharsets.UTF_8)));
    }
}
