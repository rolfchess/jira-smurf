# Sharing a chart with Google Drive

Google Drive displays an HTML file as source text. To publish a private chart as a web page, place
it in Drive and serve it with a Google Apps Script web app.

1. Save the current chart in a Drive folder as `gantt-latest.html`. Replace that file after each
   run.
2. Copy the folder ID from its Drive URL (the part after `/folders/`).
3. Create a project at [script.google.com](https://script.google.com) and paste this code, replacing
   the folder ID:

   ```javascript
   const FOLDER_ID = "PASTE_THE_FOLDER_ID_HERE";

   function doGet() {
     const files = DriveApp.getFolderById(FOLDER_ID).getFilesByName("gantt-latest.html");
     if (!files.hasNext()) {
       return HtmlService.createHtmlOutput("<p>gantt-latest.html was not found.</p>");
     }
     const html = files.next().getBlob().getDataAsString("UTF-8");
     return HtmlService.createHtmlOutput(html).setTitle("Gantt Chart");
   }
   ```

4. Choose **Deploy → New deployment → Web app**.
5. Set **Execute as** to **User accessing the web app** and restrict access to the intended group,
   such as your organization.
6. Share the web-app link.

The app reads the file on every visit, so the link always serves the latest chart. Viewers must be
able to open the Drive folder and will be asked to authorize Drive access on their first visit.

Do not use a public HTML-viewer service: planning charts contain Jira data and those services
require a public file URL.
