package android.os;

import java.io.FileDescriptor;

public class ParcelFileDescriptor {
    public FileDescriptor getFileDescriptor() { return new FileDescriptor(); }
}
