import org.mozilla.javascript.Context;
import org.mozilla.javascript.Scriptable;
import org.mozilla.javascript.WrapFactory;
import java.lang.reflect.Member;

/** Fixed Rhino 1.7.13-2 expression oracle for Java bridge behavior. */
public final class RhinoOracle {
    public static void main(String[] args) {
        Context cx = Context.enter();
        try {
            cx.setLanguageVersion(200);
            cx.setOptimizationLevel(-1);
            cx.setClassShutter(name -> !name.equals("java.lang.Class") &&
                    !name.startsWith("java.lang.Class.") &&
                    !name.equals("java.lang.ClassLoader") &&
                    !name.startsWith("java.lang.ClassLoader."));
            cx.setWrapFactory(new WrapFactory() {
                @Override public Scriptable wrapAsJavaObject(Context context, Scriptable scope,
                        Object value, Class<?> staticType) {
                    if (value instanceof Class<?> || value instanceof ClassLoader || value instanceof Member)
                        return null;
                    return super.wrapAsJavaObject(context, scope, value, staticType);
                }
            });
            Scriptable scope = cx.initStandardObjects();
            Object value = cx.evaluateString(scope, args[0], "oracle", 1, null);
            System.out.print(Context.toString(value));
        } catch (Throwable error) {
            System.out.print("ERROR:" + error.getClass().getName() + ":" + error.getMessage());
        } finally {
            Context.exit();
        }
    }
}
