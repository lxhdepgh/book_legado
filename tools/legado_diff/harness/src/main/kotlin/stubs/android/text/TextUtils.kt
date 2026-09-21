package android.text

object TextUtils {
    @JvmStatic
    fun isEmpty(str: CharSequence?): Boolean = str == null || str.isEmpty()

    @JvmStatic
    fun join(delimiter: CharSequence, tokens: Iterable<*>): String =
        tokens.filterNotNull().joinToString(delimiter.toString())
}
