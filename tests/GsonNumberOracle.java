import com.google.gson.Gson;

/** Small host-JVM oracle for BookSource fields using Gson's built-in adapters. */
public final class GsonNumberOracle {
    static final class Holder {
        long value = 7L;
        boolean primitive = true;
        Boolean nullable = true;
    }

    public static void main(String[] args) {
        Gson gson = new Gson();
        for (String value : new String[]{
                "42.0", "1.5", "9.223372036854776E18",
                "9223372036854775808", "-9223372036854775809"}) {
            try {
                Holder holder = gson.fromJson("{\"value\":\"" + value + "\"}", Holder.class);
                System.out.println("long:" + value + "=" + holder.value);
            } catch (Exception error) {
                System.out.println("long:" + value + "=" + error.getClass().getSimpleName());
            }
        }
        for (String value : new String[]{"null", "1", "\"TRUE\"", "\" true \""}) {
            try {
                Holder holder = gson.fromJson(
                        "{\"primitive\":" + value + ",\"nullable\":" + value + "}",
                        Holder.class);
                System.out.println("bool:" + value + "=" + holder.primitive + "/" + holder.nullable);
            } catch (Exception error) {
                System.out.println("bool:" + value + "=" + error.getClass().getSimpleName());
            }
        }
    }
}
