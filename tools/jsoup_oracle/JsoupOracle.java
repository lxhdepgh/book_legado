import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Base64;
import org.jsoup.Jsoup;
import org.jsoup.nodes.Document;
import org.jsoup.nodes.Element;
import org.jsoup.parser.Parser;
import org.jsoup.select.Elements;

/** Exact Jsoup 1.16.2 command-line oracle used by differential tests. */
public final class JsoupOracle {
    private static String b64(String value) {
        return Base64.getEncoder().encodeToString(value.getBytes(StandardCharsets.UTF_8));
    }

    public static void main(String[] args) throws Exception {
        String html = Files.readString(Path.of(args[1]), StandardCharsets.UTF_8);
        Document document = args[0].equals("xml")
                ? Jsoup.parse(html, "", Parser.xmlParser()) : Jsoup.parse(html);
        if (args[2].equals("document")) {
            System.out.println(b64(document.outerHtml()));
            return;
        }
        Elements elements = document.select(args[3]);
        System.out.println(elements.size());
        for (Element element : elements) {
            String value = switch (args[2]) {
                case "outer" -> element.outerHtml();
                case "html" -> element.html();
                case "text" -> element.text();
                case "ownText" -> element.ownText();
                case "data" -> element.data();
                case "id" -> element.id().isEmpty() ? element.tagName() : element.id();
                default -> throw new IllegalArgumentException(args[2]);
            };
            System.out.println(b64(value));
        }
    }
}
