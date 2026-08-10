import java.text.DecimalFormat;

/** Fixed BookHelp/StringUtils formatting oracle without Android dependencies. */
public final class BookFormatOracle {
    public static void main(String[] args) {
        String operation = args[0], value = args[1];
        switch (operation) {
            case "name" -> System.out.print(value
                    .replaceAll("\\s+作\\s*者.*|\\s+\\S+\\s+著", "").trim());
            case "author" -> System.out.print(value
                    .replaceAll("^\\s*作\\s*者[:：\\s]+|\\s+著", "").trim());
            case "word" -> {
                String result = "";
                if (value.matches("-?[0-9]+")) {
                    int words = Integer.parseInt(value);
                    if (words > 0) {
                        result = words + "字";
                        if (words > 10000)
                            result = new DecimalFormat("#.#").format(words * 1.0f / 10000f) + "万字";
                    }
                } else result = value;
                System.out.print(result);
            }
            default -> throw new IllegalArgumentException(operation);
        }
    }
}
