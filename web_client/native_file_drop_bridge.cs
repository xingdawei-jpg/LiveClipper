using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

namespace LiveClipper.NativeDrop
{
    [ComVisible(true)]
    [Guid("2B66AEE9-2195-449D-B910-2F3057F29B01")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IOleDropTarget
    {
        [PreserveSig]
        int DragEnter([MarshalAs(UnmanagedType.Interface)] IDataObject dataObject, int keyState, POINTL point, ref int effect);

        [PreserveSig]
        int DragOver(int keyState, POINTL point, ref int effect);

        [PreserveSig]
        int DragLeave();

        [PreserveSig]
        int Drop([MarshalAs(UnmanagedType.Interface)] IDataObject dataObject, int keyState, POINTL point, ref int effect);
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct POINTL
    {
        public int x;
        public int y;
    }

    [ComVisible(true)]
    [ClassInterface(ClassInterfaceType.None)]
    public sealed class FileDropTarget : IOleDropTarget
    {
        private const int S_OK = 0;
        private const int DROPEFFECT_NONE = 0;
        private const int DROPEFFECT_COPY = 1;
        private readonly Action<string[], int, int> onDrop;
        private readonly Action<string> onDiagnostic;
        private bool accepted;

        public FileDropTarget(Action<string[], int, int> onDrop, Action<string> onDiagnostic)
        {
            this.onDrop = onDrop;
            this.onDiagnostic = onDiagnostic;
        }

        public int DragEnter(IDataObject dataObject, int keyState, POINTL point, ref int effect)
        {
            accepted = HasFileDrop(dataObject);
            effect = accepted ? DROPEFFECT_COPY : DROPEFFECT_NONE;
            Notify("native OLE DragEnter: CF_HDROP=" + (accepted ? "yes" : "no"));
            return S_OK;
        }

        public int DragOver(int keyState, POINTL point, ref int effect)
        {
            effect = accepted ? DROPEFFECT_COPY : DROPEFFECT_NONE;
            return S_OK;
        }

        public int DragLeave()
        {
            accepted = false;
            return S_OK;
        }

        public int Drop(IDataObject dataObject, int keyState, POINTL point, ref int effect)
        {
            string[] paths = ExtractFileDropPaths(dataObject);
            accepted = false;
            effect = paths.Length > 0 ? DROPEFFECT_COPY : DROPEFFECT_NONE;
            Notify("native OLE Drop: CF_HDROP entries=" + paths.Length);
            if (paths.Length == 0 || onDrop == null)
            {
                return S_OK;
            }

            try
            {
                onDrop(paths, point.x, point.y);
            }
            catch (Exception exception)
            {
                Notify("native OLE callback failed: " + exception.GetType().Name);
            }
            return S_OK;
        }

        private void Notify(string message)
        {
            if (onDiagnostic == null)
            {
                return;
            }
            try
            {
                onDiagnostic(message);
            }
            catch
            {
            }
        }

        private static bool HasFileDrop(IDataObject dataObject)
        {
            if (dataObject == null)
            {
                return false;
            }
            FORMATETC format = CreateFileDropFormat();
            try
            {
                return dataObject.QueryGetData(ref format) == S_OK;
            }
            catch
            {
                return false;
            }
        }

        private static string[] ExtractFileDropPaths(IDataObject dataObject)
        {
            if (!HasFileDrop(dataObject))
            {
                return new string[0];
            }

            FORMATETC format = CreateFileDropFormat();
            STGMEDIUM medium;
            dataObject.GetData(ref format, out medium);
            try
            {
                IntPtr hDrop = medium.unionmember;
                uint count = DragQueryFile(hDrop, 0xFFFFFFFF, null, 0);
                var paths = new List<string>();
                for (uint index = 0; index < count; index++)
                {
                    uint length = DragQueryFile(hDrop, index, null, 0);
                    if (length == 0)
                    {
                        continue;
                    }
                    var buffer = new StringBuilder((int)length + 1);
                    DragQueryFile(hDrop, index, buffer, (uint)buffer.Capacity);
                    string path = buffer.ToString().Trim();
                    if (!string.IsNullOrEmpty(path))
                    {
                        paths.Add(path);
                    }
                }
                return paths.ToArray();
            }
            finally
            {
                ReleaseStgMedium(ref medium);
            }
        }

        private static FORMATETC CreateFileDropFormat()
        {
            return new FORMATETC
            {
                cfFormat = 15, // CF_HDROP
                dwAspect = DVASPECT.DVASPECT_CONTENT,
                lindex = -1,
                tymed = TYMED.TYMED_HGLOBAL,
            };
        }

        [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
        private static extern uint DragQueryFile(IntPtr hDrop, uint index, StringBuilder fileName, uint characterCount);

        [DllImport("ole32.dll")]
        private static extern void ReleaseStgMedium(ref STGMEDIUM medium);
    }

    public sealed class NativeFileDropRegistration : IDisposable
    {
        private IntPtr handle;
        private FileDropTarget target;

        internal NativeFileDropRegistration(IntPtr handle, FileDropTarget target)
        {
            this.handle = handle;
            this.target = target;
        }

        public IntPtr Handle
        {
            get { return handle; }
        }

        public void Dispose()
        {
            if (handle == IntPtr.Zero)
            {
                return;
            }
            NativeFileDropBridge.RevokeDragDrop(handle);
            handle = IntPtr.Zero;
            target = null;
        }
    }

    public static class NativeFileDropBridge
    {
        private const int S_OK = 0;

        [DllImport("ole32.dll")]
        private static extern int RegisterDragDrop(IntPtr windowHandle, [MarshalAs(UnmanagedType.Interface)] IOleDropTarget dropTarget);

        [DllImport("ole32.dll")]
        internal static extern int RevokeDragDrop(IntPtr windowHandle);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetClassName(IntPtr windowHandle, StringBuilder className, int capacity);

        private delegate bool EnumChildProc(IntPtr windowHandle, IntPtr parameter);

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool EnumChildWindows(IntPtr windowHandle, EnumChildProc callback, IntPtr parameter);

        public static IntPtr FindRenderWidgetHost(IntPtr rootHandle)
        {
            IntPtr result = IntPtr.Zero;
            EnumChildWindows(rootHandle, delegate(IntPtr handle, IntPtr parameter)
            {
                var className = new StringBuilder(128);
                GetClassName(handle, className, className.Capacity);
                if (string.Equals(className.ToString(), "Chrome_RenderWidgetHostHWND", StringComparison.Ordinal))
                {
                    result = handle;
                    return false;
                }
                return true;
            }, IntPtr.Zero);
            return result;
        }

        public static NativeFileDropRegistration Attach(
            IntPtr handle,
            Action<string[], int, int> onDrop,
            Action<string> onDiagnostic)
        {
            if (handle == IntPtr.Zero)
            {
                throw new ArgumentException("A non-zero native window handle is required.", "handle");
            }

            // WebView2 owns the original target. Revoke it before registration so
            // Windows routes CF_HDROP here instead of stripping absolute paths.
            RevokeDragDrop(handle);
            var target = new FileDropTarget(onDrop, onDiagnostic);
            int result = RegisterDragDrop(handle, target);
            if (result != S_OK)
            {
                throw new COMException("RegisterDragDrop failed.", result);
            }
            return new NativeFileDropRegistration(handle, target);
        }
    }
}
