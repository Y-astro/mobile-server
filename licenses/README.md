# Third-party frontend notices

`web/filebrowser.js` is the deployed frontend bundle of FileBrowser v2.63.23 (commit e8a388f8), modified to use six instead of two queued file transfers. Upstream FileBrowser is Apache-2.0; see filebrowser.txt and https://github.com/filebrowser/filebrowser/tree/v2.63.23. Preserve bundle notices and upstream dependency licenses when rebuilding it. It includes Ace, Vue and other frontend dependencies; the full upstream sources and dependency lockfiles identify those packages.

`web/agy_index.html` contains the existing ttyd client bundle with custom controls. ttyd is MIT; see ttyd.txt and https://github.com/tsl0922/ttyd. Its embedded xterm.js is MIT (xterm.txt); Ace in the FileBrowser bundle uses the BSD license in ace.txt. These bundled assets were inherited from the deployed server; the exact ttyd build identifier was not embedded in the snapshot.

The downloaded license texts are from the FileBrowser v2.63.23, ttyd 1.7.7, xterm.js 5.3.0 and Ace v1.43.3 upstream repositories. They do not imply those exact versions for every embedded dependency.
