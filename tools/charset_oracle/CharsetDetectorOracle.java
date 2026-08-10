package io.legado.app.lib.icu4j;

import java.nio.file.Files;
import java.nio.file.Path;

/** Command-line oracle for the exact detector bundled by Legado 3.23.110211. */
public final class CharsetDetectorOracle {
    public static void main(String[] args) throws Exception {
        byte[] input = Files.readAllBytes(Path.of(args[0]));
        CharsetMatch match = new CharsetDetector().setText(input).detect();
        System.out.print(match == null ? "UTF-8" : match.getName());
    }
}
