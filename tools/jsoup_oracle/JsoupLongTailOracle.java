import org.jsoup.Jsoup;
import org.jsoup.nodes.Document;
import org.jsoup.nodes.Element;
import org.jsoup.nodes.Node;
import org.jsoup.select.NodeFilter;

public final class JsoupLongTailOracle {
    public static void main(String[] args) {
        Document doc = Jsoup.parse("<form action='/go'><input name='a' value='1'><input name='c' checked></form><p>x<b>y</b></p>", "https://host/base");
        Element p = doc.selectFirst("p");
        System.out.println("sourceRange=" + p.sourceRange() + "|" + p.sourceRange().isTracked());
        System.out.println("endSourceRange=" + p.endSourceRange() + "|" + p.endSourceRange().isTracked());
        System.out.println("parser=" + doc.parser());
        System.out.println("quirks=" + doc.quirksMode());
        System.out.println("formData=" + doc.forms().get(0).formData());
        StringBuilder order = new StringBuilder();
        p.traverse((node, depth) -> order.append(node.nodeName()).append('@').append(depth).append(','));
        System.out.println("forEach=" + order);

        Document longDoc = Jsoup.parse("<form id=f><input name=r type=reset value=R>"
                + "<input name=file type=file value=F><button name=b value=B></button>"
                + "<button name=x type=button value=X></button><input name=c type=checkbox checked>"
                + "<select name=s multiple><option value=one>1</option><option value=two>2</option></select>"
                + "</form><input form=f name=outside value=O>"
                + "<p><span><em>x</em></span><b>y</b><i>z</i></p>", "https://host/");
        Element filtered = longDoc.selectFirst("p");
        StringBuilder lifecycle = new StringBuilder();
        filtered.filter(new NodeFilter() {
            public FilterResult head(Node node, int depth) {
                lifecycle.append("h:").append(node.nodeName()).append(depth).append(',');
                if (node.nodeName().equals("span")) return FilterResult.SKIP_CHILDREN;
                if (node.nodeName().equals("b")) return FilterResult.SKIP_ENTIRELY;
                if (node.nodeName().equals("i")) return FilterResult.REMOVE;
                return FilterResult.CONTINUE;
            }
            public FilterResult tail(Node node, int depth) {
                lifecycle.append("t:").append(node.nodeName()).append(depth).append(',');
                return FilterResult.CONTINUE;
            }
        });
        System.out.println("formElements=" + longDoc.forms().get(0).elements().size());
        System.out.println("formLong=" + longDoc.forms().get(0).formData());
        System.out.println("filter=" + lifecycle + "|" + filtered.text() + "|" + filtered.select("i").size());
    }

    private interface Call { String run() throws Exception; }
    private static String catchValue(Call call) {
        try { return call.run(); } catch (Throwable error) {
            return "ERROR:" + error.getClass().getSimpleName() + ":" + error.getMessage();
        }
    }
}
