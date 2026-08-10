package android.system;

import java.io.FileDescriptor;

public final class Os {
    public static long lseek(FileDescriptor fd, long offset, int whence) { return offset; }
    public static int read(FileDescriptor fd, byte[] data, int offset, int count) { return 0; }
}
